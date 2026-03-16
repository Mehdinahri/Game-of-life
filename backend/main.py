"""
backend/main.py
===============
API FastAPI pour le Game of Life distribué.

Endpoints
---------
GET  /health          Santé du service + statut des 3 Redis
GET  /state           Snapshot synchrone : toutes les cellules vivantes des 3 zones
GET  /state/meta      Méta-données : génération courante, cellules vivantes par zone
WS   /ws              WebSocket : diffuse l'état complet dès qu'une nouvelle génération est prête

Architecture
------------
                      ┌──────────────────────────────────────────┐
                      │              FastAPI (backend)           │
  Frontend ──WS──────▶│  ConnectionManager                       │
  Frontend ──GET─────▶│  /state                                  │
                      │       │          │          │            │
                      │   redis-1     redis-2     redis-3        │
                      │  (zone-1)    (zone-2)    (zone-3)        │
                      └──────────────────────────────────────────┘

La détection de nouvelles générations utilise le Pub/Sub Redis :
- Chaque worker publie sur le channel `gol:tick` après chaque génération.
- Le backend écoute ce channel avec un subscriber dédié (tâche asyncio).
- Dès réception, il lit les 3 Redis en parallèle (asyncio.gather) et
  diffuse le JSON à tous les clients WebSocket connectés.

Variables d'environnement
--------------------------
REDIS_1_HOST   hostname Redis zone-1  (défaut: redis-1)
REDIS_2_HOST   hostname Redis zone-2  (défaut: redis-2)
REDIS_3_HOST   hostname Redis zone-3  (défaut: redis-3)
REDIS_PORT     port commun             (défaut: 6379)
WS_THROTTLE_MS intervalle min entre deux pushes WebSocket en ms (défaut: 100)
CORS_ORIGINS   origines autorisées CORS, séparées par , (défaut: *)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ── Import des utilitaires partagés ──────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.utils import (
    GRID_HEIGHT,
    GRID_WIDTH,
    ZONE_DEFINITIONS,
    ZONE_ORDER,
    cell_key,
    generation_key,
)

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

REDIS_PORT      = int(os.environ.get("REDIS_PORT", 6379))
WS_THROTTLE_MS  = int(os.environ.get("WS_THROTTLE_MS", 100))
CORS_ORIGINS    = os.environ.get("CORS_ORIGINS", "*").split(",")

# Map zone → (host, x_start, x_end)
ZONE_REDIS: dict[str, dict] = {
    zone: {
        "host":    os.environ.get(defn["redis_host_env"], zone),
        "port":    REDIS_PORT,
        "x_start": defn["x_start"],
        "x_end":   defn["x_end"],
    }
    for zone, defn in ZONE_DEFINITIONS.items()
}

# Channel Pub/Sub utilisé par les workers pour signaler un nouveau tick
PUBSUB_CHANNEL = "gol:tick"

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("gol.backend")


# ──────────────────────────────────────────────────────────────────────────────
# Pool de connexions Redis (async)
# ──────────────────────────────────────────────────────────────────────────────

class RedisPool:
    """
    Gère un pool de connexions async vers les 3 instances Redis.

    Utilise redis.asyncio pour ne jamais bloquer la boucle d'événements FastAPI.
    Un client dédié (subscriber) est créé pour le Pub/Sub afin de ne pas
    mélanger les commandes normales et le mode SUBSCRIBE.
    """

    def __init__(self) -> None:
        # Clients de lecture/écriture, un par zone
        self._clients:    dict[str, aioredis.Redis] = {}
        # Client dédié Pub/Sub (écoute sur redis-1 par convention)
        self._subscriber: aioredis.Redis | None = None

    def _make(self, host: str, port: int) -> aioredis.Redis:
        return aioredis.Redis(
            host=host,
            port=port,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            retry_on_timeout=True,
            health_check_interval=30,
        )

    async def connect(self) -> None:
        """Ouvre toutes les connexions Redis."""
        for zone, cfg in ZONE_REDIS.items():
            self._clients[zone] = self._make(cfg["host"], cfg["port"])
            log.info("Redis pool créé : %s → %s:%d", zone, cfg["host"], cfg["port"])

        # Subscriber sur redis-1 (les workers peuvent publier sur n'importe lequel)
        self._subscriber = self._make(
            ZONE_REDIS["zone-1"]["host"], ZONE_REDIS["zone-1"]["port"]
        )

    async def close(self) -> None:
        """Ferme proprement toutes les connexions."""
        for client in self._clients.values():
            await client.aclose()
        if self._subscriber:
            await self._subscriber.aclose()

    def client(self, zone: str) -> aioredis.Redis:
        """Retourne le client async pour une zone donnée."""
        if zone not in self._clients:
            raise KeyError(f"Zone inconnue : {zone}")
        return self._clients[zone]

    def all_clients(self) -> list[tuple[str, aioredis.Redis]]:
        """Liste de (zone_name, client) dans l'ordre ZONE_ORDER."""
        return [(z, self._clients[z]) for z in ZONE_ORDER]

    async def ping_all(self) -> dict[str, bool]:
        """Vérifie la connectivité vers chaque Redis. Retourne {zone: ok}."""
        results: dict[str, bool] = {}
        for zone, client in self.all_clients():
            try:
                await client.ping()
                results[zone] = True
            except Exception:
                results[zone] = False
        return results

    @property
    def subscriber(self) -> aioredis.Redis:
        assert self._subscriber is not None, "Pool non initialisé"
        return self._subscriber


# Instance globale du pool
pool = RedisPool()


# ──────────────────────────────────────────────────────────────────────────────
# Gestionnaire de connexions WebSocket
# ──────────────────────────────────────────────────────────────────────────────

class ConnectionManager:
    """
    Registre des clients WebSocket connectés.

    Thread-safety : FastAPI tourne dans un seul thread async, les accès
    au set `_connections` ne nécessitent pas de Lock.
    """

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections.add(ws)
        log.info("WS connecté  — %d client(s) actif(s)", len(self._connections))

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(ws)
        log.info("WS déconnecté — %d client(s) actif(s)", len(self._connections))

    async def broadcast(self, payload: str) -> None:
        """
        Envoie `payload` à tous les clients connectés.

        Les déconnexions silencieuses sont gérées : un client mort est retiré
        du registre sans lever d'exception.
        """
        if not self._connections:
            return

        dead: list[WebSocket] = []

        async with self._lock:
            targets = list(self._connections)

        results = await asyncio.gather(
            *[ws.send_text(payload) for ws in targets],
            return_exceptions=True,
        )

        for ws, result in zip(targets, results):
            if isinstance(result, Exception):
                log.debug("WS mort détecté, retrait : %s", result)
                dead.append(ws)

        if dead:
            async with self._lock:
                for ws in dead:
                    self._connections.discard(ws)

    @property
    def count(self) -> int:
        return len(self._connections)


manager = ConnectionManager()


# ──────────────────────────────────────────────────────────────────────────────
# Lecture de l'état des cellules
# ──────────────────────────────────────────────────────────────────────────────

async def fetch_alive_cells_from_zone(
    zone: str,
    client: aioredis.Redis,
) -> list[list[int]]:
    """
    Récupère toutes les cellules vivantes d'une zone via SCAN + pipeline GET.

    Stratégie :
      1. SCAN itératif avec pattern `cell:*` pour ne récupérer que les clés
         existantes (les cellules mortes sont supprimées par le worker via DEL).
      2. Les clés trouvées sont parsées directement (format `cell:X:Y`) — pas
         besoin de GET si on fait confiance au fait que seules les cellules
         vivantes sont stockées (valeur toujours 1).

    Complexité : O(vivantes) au lieu de O(30 000) pour un MGET full-zone.
    """
    cells: list[list[int]] = []
    cursor = 0

    while True:
        cursor, keys = await client.scan(cursor, match="cell:*", count=500)
        for key in keys:
            # key = "cell:X:Y"
            parts = key.split(":")
            if len(parts) == 3:
                try:
                    x, y = int(parts[1]), int(parts[2])
                    # Validation : la clé appartient bien à cette zone
                    z_cfg = ZONE_REDIS[zone]
                    if z_cfg["x_start"] <= x <= z_cfg["x_end"] and 0 <= y < GRID_HEIGHT:
                        cells.append([x, y])
                except ValueError:
                    pass  # clé malformée → ignorée

        if cursor == 0:
            break

    return cells


async def fetch_all_zones() -> tuple[list[list[int]], dict[str, int], int]:
    """
    Interroge les 3 Redis en parallèle et fusionne les résultats.

    Returns
    -------
    cells       : liste de toutes les cellules vivantes [[x,y], ...]
    per_zone    : {zone_name: nb_vivantes}
    total_alive : total cellules vivantes
    """
    tasks = [
        fetch_alive_cells_from_zone(zone, client)
        for zone, client in pool.all_clients()
    ]

    # asyncio.gather → les 3 lectures Redis se font en parallèle
    zone_results: list[list[list[int]]] = await asyncio.gather(*tasks)

    cells: list[list[int]] = []
    per_zone: dict[str, int] = {}

    for zone_name, zone_cells in zip(ZONE_ORDER, zone_results):
        cells.extend(zone_cells)
        per_zone[zone_name] = len(zone_cells)

    return cells, per_zone, len(cells)


async def fetch_current_generation() -> int:
    """
    Estime la génération courante en interrogeant redis-1.
    Retourne -1 si aucune information n'est disponible.
    """
    client = pool.client("zone-1")
    # On cherche la dernière clé generation:*:done
    cursor = 0
    max_gen = -1
    while True:
        cursor, keys = await client.scan(cursor, match="generation:*:done", count=200)
        for key in keys:
            try:
                gen = int(key.split(":")[1])
                if gen > max_gen:
                    max_gen = gen
            except (ValueError, IndexError):
                pass
        if cursor == 0:
            break
    return max_gen


# ──────────────────────────────────────────────────────────────────────────────
# Tâche de fond : écoute Pub/Sub + diffusion WebSocket
# ──────────────────────────────────────────────────────────────────────────────

async def _pubsub_broadcaster() -> None:
    """
    Tâche asyncio longue durée qui écoute le channel `gol:tick`.

    Flux :
      Worker → PUBLISH gol:tick <generation>
      Backend ← message reçu
      Backend → lit les 3 Redis en parallèle
      Backend → broadcast JSON à tous les clients WebSocket

    Throttling : WS_THROTTLE_MS évite les rafales si les workers publient
    plus vite que les clients ne peuvent consommer.
    """
    log.info("Démarrage du broadcaster Pub/Sub (channel: %s)", PUBSUB_CHANNEL)
    last_broadcast = 0.0
    throttle = WS_THROTTLE_MS / 1000.0

    # Retry infini : reconnexion auto si le subscriber perd la connexion
    while True:
        try:
            pubsub = pool.subscriber.pubsub()
            await pubsub.subscribe(PUBSUB_CHANNEL)
            log.info("Abonné au channel Redis '%s'", PUBSUB_CHANNEL)

            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue

                # Throttle : on n'envoie pas plus souvent que WS_THROTTLE_MS
                now = time.monotonic()
                if now - last_broadcast < throttle:
                    continue
                last_broadcast = now

                # Pas de clients connectés → pas la peine de lire Redis
                if manager.count == 0:
                    continue

                try:
                    cells, per_zone, total = await fetch_all_zones()
                    generation = message.get("data", "?")
                    payload = json.dumps({
                        "type":       "state",
                        "generation": generation,
                        "total_alive": total,
                        "per_zone":   per_zone,
                        "cells":      cells,
                    })
                    await manager.broadcast(payload)
                    log.debug(
                        "Broadcast gen=%s  vivantes=%d  clients=%d",
                        generation, total, manager.count,
                    )

                except Exception as exc:
                    log.error("Erreur lors du fetch/broadcast : %s", exc)

        except Exception as exc:
            log.warning("Subscriber déconnecté (%s) — reconnexion dans 2s …", exc)
            await asyncio.sleep(2)


# ──────────────────────────────────────────────────────────────────────────────
# Lifecycle FastAPI
# ──────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialisation et teardown de l'application."""
    log.info("Démarrage du backend Game of Life …")
    await pool.connect()

    # Lancement de la tâche Pub/Sub en arrière-plan
    broadcaster_task = asyncio.create_task(_pubsub_broadcaster())
    log.info("Backend prêt.")

    yield  # ← l'application tourne ici

    log.info("Arrêt du backend …")
    broadcaster_task.cancel()
    try:
        await broadcaster_task
    except asyncio.CancelledError:
        pass
    await pool.close()
    log.info("Backend arrêté proprement.")


# ──────────────────────────────────────────────────────────────────────────────
# Application FastAPI
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Game of Life — Backend distribué",
    description=(
        "API REST + WebSocket pour le Game of Life distribué sur 3 instances Redis. "
        "Chaque zone (100×300 colonnes) est gérée par un worker dédié."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — autoriser le frontend React
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS if CORS_ORIGINS != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ──────────────────────────────────────────────────────────────────────────────
# Routes REST
# ──────────────────────────────────────────────────────────────────────────────

@app.get(
    "/health",
    summary="Santé du service",
    response_description="Statut du backend et de chaque instance Redis",
)
async def health() -> dict[str, Any]:
    """
    Vérifie la connectivité vers les 3 instances Redis.

    Retourne `200 OK` si au moins une instance répond,
    `503 Service Unavailable` si toutes sont hors ligne.
    """
    redis_status = await pool.ping_all()
    all_down = not any(redis_status.values())

    response: dict[str, Any] = {
        "status": "degraded" if not all(redis_status.values()) else "ok",
        "redis":  {
            zone: {"host": ZONE_REDIS[zone]["host"], "reachable": ok}
            for zone, ok in redis_status.items()
        },
        "websocket_clients": manager.count,
    }

    if all_down:
        return JSONResponse(content=response, status_code=503)
    return response


@app.get(
    "/state",
    summary="Snapshot de l'état complet de la grille",
    response_description="Liste de toutes les cellules vivantes",
)
async def get_state() -> dict[str, Any]:
    """
    Interroge les 3 instances Redis **en parallèle** et retourne l'union
    de toutes les cellules vivantes.

    Les 3 lectures Redis se déroulent simultanément grâce à `asyncio.gather`,
    ce qui réduit la latence au maximum des 3 temps individuels plutôt qu'à
    leur somme.

    Réponse :
    ```json
    {
      "cells": [[x1, y1], [x2, y2], ...],
      "total_alive": 4231,
      "per_zone": { "zone-1": 1402, "zone-2": 1428, "zone-3": 1401 }
    }
    ```
    """
    try:
        cells, per_zone, total = await fetch_all_zones()
        return {
            "cells":       cells,
            "total_alive": total,
            "per_zone":    per_zone,
        }
    except Exception as exc:
        log.error("/state – erreur : %s", exc)
        raise HTTPException(status_code=503, detail=f"Erreur Redis : {exc}") from exc


@app.get(
    "/state/meta",
    summary="Méta-données de la simulation",
    response_description="Génération courante, compteurs, configuration de la grille",
)
async def get_state_meta() -> dict[str, Any]:
    """
    Retourne les méta-données de la simulation sans transmettre les coordonnées
    de chaque cellule — utile pour le monitoring ou l'en-tête du frontend.
    """
    try:
        _, per_zone, total = await fetch_all_zones()
        generation          = await fetch_current_generation()
        redis_status        = await pool.ping_all()

        return {
            "generation":        generation,
            "total_alive":       total,
            "per_zone":          per_zone,
            "grid": {
                "width":  GRID_WIDTH,
                "height": GRID_HEIGHT,
                "zones":  [
                    {
                        "name":    zone,
                        "x_start": ZONE_REDIS[zone]["x_start"],
                        "x_end":   ZONE_REDIS[zone]["x_end"],
                        "redis":   ZONE_REDIS[zone]["host"],
                        "online":  redis_status.get(zone, False),
                    }
                    for zone in ZONE_ORDER
                ],
            },
            "websocket_clients": manager.count,
        }
    except Exception as exc:
        log.error("/state/meta – erreur : %s", exc)
        raise HTTPException(status_code=503, detail=f"Erreur Redis : {exc}") from exc



# ──────────────────────────────────────────────────────────────────────────────
# Reset endpoint
# ──────────────────────────────────────────────────────────────────────────────

@app.post(
    "/reset",
    summary="Vider toutes les zones et générer un nouvel état aléatoire",
    response_description="Confirmation du reset avec nombre de cellules créées",
)
async def reset_simulation() -> dict[str, Any]:
    """
    Vide les 3 instances Redis (FLUSHDB) puis génère un seed aléatoire
    dans chaque zone via un pipeline.

    Les workers détecteront que leur zone est vide au prochain tick et
    effectueront eux-mêmes le re-seed. Ici on le fait directement depuis
    le backend pour une réponse immédiate au frontend.

    La grille sera visible après le prochain tick du worker (~TICK_INTERVAL s).
    """
    import random

    GRID_W, GRID_H = 60, 50
    SEED_DENSITY   = 0.01   # ~900 cellules vivantes initiales

    try:
        async def reset_zone(zone: str, client: aioredis.Redis) -> int:
            z_cfg   = ZONE_REDIS[zone]
            x_start = z_cfg["x_start"]
            x_end   = z_cfg["x_end"]

            # 1. Flush tous les états et ghost columns de cette instance
            await client.flushdb()

            # 2. Re-seed aléatoire via pipeline
            pipe  = client.pipeline(transaction=False)
            alive = 0
            for x in range(x_start, x_end + 1):
                for y in range(GRID_H):
                    if random.random() < SEED_DENSITY:
                        pipe.set(f"cell:{x}:{y}", 1)
                        alive += 1
            await pipe.execute()
            log.info("Reset zone=%s  cellules_vivantes=%d", zone, alive)
            return alive

        # Reset les 3 zones en parallèle
        results = await asyncio.gather(
            *[reset_zone(zone, client) for zone, client in pool.all_clients()],
            return_exceptions=True,
        )

        per_zone: dict[str, Any] = {}
        total = 0
        for zone, result in zip(ZONE_ORDER, results):
            if isinstance(result, Exception):
                per_zone[zone] = {"error": str(result)}
                log.error("Reset %s échoué : %s", zone, result)
            else:
                per_zone[zone] = result
                total += result

        # Notifier les clients WebSocket que la grille a été réinitialisée
        await manager.broadcast(
            __import__("json").dumps({"type": "reset", "message": "Grid reseeded"})
        )

        return {
            "ok":        True,
            "message":   "Simulation réinitialisée",
            "total_alive": total,
            "per_zone":  per_zone,
        }

    except Exception as exc:
        log.error("/reset – erreur : %s", exc)
        raise HTTPException(status_code=503, detail=f"Reset échoué : {exc}") from exc


# ──────────────────────────────────────────────────────────────────────────────
# Stop / Start / Set-cells endpoints
# ──────────────────────────────────────────────────────────────────────────────

PAUSE_KEY = "gol:paused"


@app.post(
    "/stop",
    summary="Mettre en pause la simulation",
    response_description="Confirmation de la mise en pause",
)
async def stop_simulation() -> dict[str, Any]:
    """
    Met en pause la simulation en écrivant `gol:paused=1` dans les 3 Redis.
    Chaque worker vérifie cette clé dans son propre Redis (redis-1/2/3).
    """
    try:
        # Écrire la clé de pause dans les 3 Redis en parallèle
        await asyncio.gather(*[
            pool.client(zone).set(PAUSE_KEY, "1", ex=3600)
            for zone in ZONE_ORDER
        ])
        await manager.broadcast(
            json.dumps({"type": "paused", "message": "Simulation mise en pause"})
        )
        log.info("Simulation mise en pause (3 Redis mis à jour).")
        return {"ok": True, "message": "Simulation mise en pause"}
    except Exception as exc:
        log.error("/stop – erreur : %s", exc)
        raise HTTPException(status_code=503, detail=f"Stop échoué : {exc}") from exc


@app.post(
    "/start",
    summary="Reprendre / démarrer la simulation",
    response_description="Confirmation du démarrage",
)
async def start_simulation() -> dict[str, Any]:
    """
    Reprend la simulation en supprimant la clé `gol:paused` dans les 3 Redis.
    """
    try:
        # Supprimer la clé de pause dans les 3 Redis en parallèle
        await asyncio.gather(*[
            pool.client(zone).delete(PAUSE_KEY)
            for zone in ZONE_ORDER
        ])
        await manager.broadcast(
            json.dumps({"type": "running", "message": "Simulation démarrée"})
        )
        log.info("Simulation démarrée / reprise (3 Redis mis à jour).")
        return {"ok": True, "message": "Simulation démarrée"}
    except Exception as exc:
        log.error("/start – erreur : %s", exc)
        raise HTTPException(status_code=503, detail=f"Start échoué : {exc}") from exc


@app.post(
    "/set-cells",
    summary="Définir l'état initial de la grille avec des cellules choisies par l'utilisateur",
    response_description="Confirmation avec nombre de cellules écrites",
)
async def set_cells(body: dict[str, Any]) -> dict[str, Any]:
    """
    Reçoit une liste de cellules `[[x, y], ...]`, vide les 3 Redis (FLUSHDB),
    puis écrit chaque cellule dans l'instance Redis correspondant à sa zone.

    Zone assignment :
      x in [0,   99]  → zone-1 (redis-1)
      x in [100, 199] → zone-2 (redis-2)
      x in [200, 299] → zone-3 (redis-3)
    """
    cells_raw = body.get("cells", [])
    if not isinstance(cells_raw, list):
        raise HTTPException(status_code=422, detail="'cells' doit être une liste de [x, y]")

    # Valider et regrouper les cellules par zone
    by_zone: dict[str, list[tuple[int, int]]] = {z: [] for z in ZONE_ORDER}
    for item in cells_raw:
        try:
            x, y = int(item[0]), int(item[1])
        except (TypeError, ValueError, IndexError):
            continue  # ignorer les entrées malformées
        if not (0 <= x < GRID_WIDTH and 0 <= y < GRID_HEIGHT):
            continue  # hors grille → ignoré
        # Trouver la zone
        for zone, defn in ZONE_REDIS.items():
            if defn["x_start"] <= x <= defn["x_end"]:
                by_zone[zone].append((x, y))
                break

    try:
        # 1. Flush les 3 Redis en parallèle
        await asyncio.gather(*[
            pool.client(zone).flushdb()
            for zone in ZONE_ORDER
        ])

        # 2. Écrire les cellules dans chaque Redis via pipeline
        async def write_zone(zone: str, cells: list[tuple[int, int]]) -> int:
            if not cells:
                return 0
            client = pool.client(zone)
            pipe   = client.pipeline(transaction=False)
            for (x, y) in cells:
                pipe.set(f"cell:{x}:{y}", 1)
            await pipe.execute()
            log.info("set-cells zone=%s  cellules=%d", zone, len(cells))
            return len(cells)

        results = await asyncio.gather(*[
            write_zone(zone, by_zone[zone])
            for zone in ZONE_ORDER
        ])

        total = sum(results)
        per_zone = {zone: n for zone, n in zip(ZONE_ORDER, results)}

        # 3. Notifier les clients WebSocket
        cells_snap, pz, tot = await fetch_all_zones()
        await manager.broadcast(json.dumps({
            "type":        "state",
            "generation":  0,
            "total_alive": tot,
            "per_zone":    pz,
            "cells":       cells_snap,
        }))

        return {
            "ok":          True,
            "total_alive": total,
            "per_zone":    per_zone,
        }

    except Exception as exc:
        log.error("/set-cells – erreur : %s", exc)
        raise HTTPException(status_code=503, detail=f"set-cells échoué : {exc}") from exc


# ──────────────────────────────────────────────────────────────────────────────
# WebSocket
# ──────────────────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """
    WebSocket de diffusion en temps réel.

    Protocole :
      → Connexion acceptée
      ← Envoi immédiat de l'état courant (message type: "state")
      ← Nouveaux messages à chaque génération détectée via Pub/Sub
      → Message client "ping" → réponse "pong" (keepalive applicatif)
      → Déconnexion propre gérée

    Exemple de message reçu par le client :
    ```json
    {
      "type": "state",
      "generation": 142,
      "total_alive": 4231,
      "per_zone": { "zone-1": 1402, "zone-2": 1428, "zone-3": 1401 },
      "cells": [[12, 45], [13, 45], ...]
    }
    ```
    """
    await manager.connect(ws)

    try:
        # Snapshot immédiat dès la connexion pour que le client n'attende pas
        # le prochain tick du worker avant de voir quelque chose.
        try:
            cells, per_zone, total = await fetch_all_zones()
            generation = await fetch_current_generation()
            await ws.send_text(json.dumps({
                "type":        "state",
                "generation":  generation,
                "total_alive": total,
                "per_zone":    per_zone,
                "cells":       cells,
            }))
        except Exception as exc:
            log.warning("Impossible d'envoyer le snapshot initial : %s", exc)
            await ws.send_text(json.dumps({
                "type":  "error",
                "detail": f"Redis indisponible : {exc}",
            }))

        # Boucle de réception — gère le ping/pong applicatif et détecte
        # les déconnexions propres envoyées par le client.
        while True:
            try:
                data = await asyncio.wait_for(ws.receive_text(), timeout=30.0)
                if data.strip().lower() == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
            except asyncio.TimeoutError:
                # Pas de message client depuis 30 s — on envoie un heartbeat
                await ws.send_text(json.dumps({"type": "heartbeat"}))
            except WebSocketDisconnect:
                break

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.error("Erreur WebSocket inattendue : %s", exc)
    finally:
        await manager.disconnect(ws)
