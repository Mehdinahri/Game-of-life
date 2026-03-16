"""
worker/main.py
==============
Point d'entrée du worker Game of Life distribué.

Responsabilités :
  1. Connexion à Redis propre (own + voisins) avec retry exponentiel.
  2. Seed aléatoire de la zone si la grille est vierge (1ère instance).
  3. Synchronisation inter-zones via "ghost columns" stockées dans Redis.
  4. Calcul de la génération suivante (règles de Conway).
  5. Sauvegarde atomique du nouvel état + publication d'un signal de tick.
  6. Boucle infinie cadencée par TICK_INTERVAL (env var, défaut 0.5 s).

Variables d'environnement attendues (injectées par docker-compose) :
  GRID_ZONE        zone-1 | zone-2 | zone-3
  REDIS_HOST       hostname Redis de cette zone (ex: redis-1)
  REDIS_PORT       port Redis (défaut: 6379)
  REDIS_1_HOST     hostname Redis zone-1  (pour lecture cross-zone)
  REDIS_2_HOST     hostname Redis zone-2
  REDIS_3_HOST     hostname Redis zone-3
  TICK_INTERVAL    secondes entre deux générations (défaut: 0.5)
  SEED_DENSITY     probabilité qu'une cellule soit vivante au départ (défaut: 0.3)
  SYNC_TIMEOUT     secondes max d'attente pour les ghost columns (défaut: 2.0)
"""

from __future__ import annotations

import logging
import os
import random
import sys
import time
from pathlib import Path
from typing import Optional

import redis
import redis.exceptions

# ── Résolution des imports ────────────────────────────────────────────────────
# Fonctionne depuis : `python worker/main.py`  ET  `python main.py` (dans /worker)
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.utils import (
    GRID_HEIGHT,
    GRID_WIDTH,
    ZoneConfig,
    cell_key,
    generation_key,
    get_zone_config,
    ghost_col_key,
)

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

TICK_INTERVAL: float   = float(os.environ.get("TICK_INTERVAL",  "0.5"))
SEED_DENSITY:  float   = float(os.environ.get("SEED_DENSITY",   "0.3"))
SYNC_TIMEOUT:  float   = float(os.environ.get("SYNC_TIMEOUT",   "2.0"))
GHOST_KEY_TTL: int     = 10          # secondes — les ghost columns expirent auto
REDIS_CONNECT_RETRIES  = 12
REDIS_RETRY_BASE_DELAY = 1.0         # secondes, doublé à chaque tentative

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("gol.worker")


# ──────────────────────────────────────────────────────────────────────────────
# 1. Connexion Redis
# ──────────────────────────────────────────────────────────────────────────────

def _make_client(host: str, port: int) -> redis.Redis:
    """Crée un client Redis sans encore tester la connexion."""
    return redis.Redis(
        host=host,
        port=port,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=3,
        retry_on_timeout=True,
    )


def connect_with_retry(host: str, port: int, label: str = "") -> redis.Redis:
    """
    Tente de se connecter à Redis avec un backoff exponentiel plafonné à 30 s.

    Utile car docker-compose démarre les conteneurs en quasi-simultané et Redis
    peut ne pas être prêt immédiatement (malgré `healthcheck: condition: service_healthy`).
    """
    tag = label or f"{host}:{port}"
    delay = REDIS_RETRY_BASE_DELAY

    for attempt in range(1, REDIS_CONNECT_RETRIES + 1):
        try:
            client = _make_client(host, port)
            client.ping()
            log.info("✓ Redis connecté : %s  (tentative %d)", tag, attempt)
            return client
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
            if attempt == REDIS_CONNECT_RETRIES:
                raise RuntimeError(
                    f"Impossible de joindre Redis {tag} après {attempt} tentatives."
                ) from exc
            log.warning(
                "Redis %s indisponible (tentative %d/%d) — retry dans %.1fs …",
                tag, attempt, REDIS_CONNECT_RETRIES, delay,
            )
            time.sleep(delay)
            delay = min(delay * 2, 30.0)   # backoff exponentiel, max 30 s

    # Jamais atteint (le raise ci-dessus couvre le dernier tour), mais mypy l'exige.
    raise RuntimeError("Unreachable")


# ──────────────────────────────────────────────────────────────────────────────
# 2. Seed — initialisation aléatoire de la grille
# ──────────────────────────────────────────────────────────────────────────────

def _zone_is_empty(r: redis.Redis, cfg: ZoneConfig) -> bool:
    """Retourne True si aucune cellule de la zone n'est encore écrite dans Redis."""
    # On ne teste qu'une cellule représentative (centre de la zone) pour éviter
    # un scan coûteux sur 30 000 clés.
    probe_x = (cfg.x_start + cfg.x_end) // 2
    probe_y = GRID_HEIGHT // 2
    return r.get(cell_key(probe_x, probe_y)) is None


def seed_zone(r: redis.Redis, cfg: ZoneConfig, density: float = SEED_DENSITY) -> int:
    """
    Remplit la zone avec un état initial aléatoire.

    Utilise un pipeline Redis pour envoyer toutes les clés en un seul aller-retour.
    Retourne le nombre de cellules vivantes créées.
    """
    log.info(
        "Seeding %s (%d cellules, densité=%.0f%%) …",
        cfg.zone_name, cfg.total_cells, density * 100,
    )
    alive_count = 0
    pipe = r.pipeline(transaction=False)

    for x, y in cfg.iter_cells():
        state = 1 if random.random() < density else 0
        pipe.set(cell_key(x, y), state)
        alive_count += state

    pipe.execute()
    log.info(
        "Seed terminé : %d/%d cellules vivantes (%.1f%%)",
        alive_count, cfg.total_cells, alive_count / cfg.total_cells * 100,
    )
    return alive_count


# ──────────────────────────────────────────────────────────────────────────────
# 3. Chargement / sauvegarde de l'état
# ──────────────────────────────────────────────────────────────────────────────

# Type alias pour la carte d'état : {(x, y): 0|1}
CellMap = dict[tuple[int, int], int]


def load_zone(r: redis.Redis, cfg: ZoneConfig) -> CellMap:
    """
    Charge l'état de toutes les cellules de la zone via un pipeline MGET.

    Toute clé absente est interprétée comme morte (0) — cohérent avec le seed
    paresseux et les bords de grille.
    """
    coords = list(cfg.iter_cells())

    # Pipeline : une seule aller-retour réseau pour 30 000 GET
    pipe = r.pipeline(transaction=False)
    for x, y in coords:
        pipe.get(cell_key(x, y))
    raw_values = pipe.execute()

    return {
        coord: (int(v) if v is not None else 0)
        for coord, v in zip(coords, raw_values)
    }


def save_zone(r: redis.Redis, state: CellMap) -> None:
    """
    Sauvegarde l'état complet de la zone via un pipeline SET.

    On écrit uniquement les cellules vivantes (SET key 1) et on supprime les
    mortes (DEL key) pour garder Redis propre et le DBSIZE stable.
    """
    pipe = r.pipeline(transaction=False)
    for (x, y), alive in state.items():
        if alive:
            pipe.set(cell_key(x, y), 1)
        else:
            # DEL sur une clé inexistante est un no-op côté Redis → pas de risque
            pipe.delete(cell_key(x, y))
    pipe.execute()


# ──────────────────────────────────────────────────────────────────────────────
# 4. Ghost columns — synchronisation aux frontières inter-zones
# ──────────────────────────────────────────────────────────────────────────────
#
# Problème : pour calculer si une cellule à x=99 (bord droit de zone-1) doit
# vivre ou mourir, on a besoin de l'état des cellules x=100 (bord gauche de
# zone-2), qui appartiennent à un autre worker/Redis.
#
# Solution : chaque worker publie ses deux colonnes de bord dans son propre
# Redis sous des clés `ghost:col:<x>` (chaîne de 300 chars '0'/'1').
# Les workers voisins lisent ces clés directement sur l'instance Redis adverse.
#
#  zone-1 Redis           zone-2 Redis
#  ──────────────         ──────────────
#  ghost:col:99  ←─ lu ─  (worker-2 lit sur redis-1)
#  (worker-1 lit sur redis-2) ─ lu ─→  ghost:col:100
#

def publish_ghost_columns(r: redis.Redis, cfg: ZoneConfig, state: CellMap) -> None:
    """
    Écrit les colonnes de bord gauche et droite dans le Redis de cette zone.

    TTL court (GHOST_KEY_TTL) : si le worker plante, les voisins reçoivent
    automatiquement une colonne vide après quelques secondes plutôt que de lire
    un état périmé.
    """
    pipe = r.pipeline(transaction=False)
    for col in (cfg.x_start, cfg.x_end):
        col_str = "".join(
            str(state.get((col, y), 0))
            for y in range(cfg.y_start, cfg.y_end + 1)
        )
        pipe.set(ghost_col_key(col), col_str, ex=GHOST_KEY_TTL)
    pipe.execute()


def fetch_ghost_column(
    neighbour_redis: redis.Redis,
    ghost_x: int,
    cfg: ZoneConfig,
    timeout: float = SYNC_TIMEOUT,
) -> CellMap:
    """
    Lit la ghost column `ghost_x` sur l'instance Redis d'un voisin.

    Si la clé est absente (voisin pas encore prêt), on attend jusqu'à `timeout`
    secondes en boucle courte, puis on tombe sur une colonne toute morte.
    Cela permet au voisin d'avoir un peu de délai sans bloquer indéfiniment.

    Returns
    -------
    CellMap : { (ghost_x, y): 0|1, ... }  pour y dans [y_start, y_end]
    """
    deadline = time.monotonic() + timeout
    poll_interval = 0.05  # 50 ms

    while True:
        try:
            raw = neighbour_redis.get(ghost_col_key(ghost_x))
        except redis.exceptions.ConnectionError:
            log.warning("Impossible de lire ghost:col:%d — voisin injoignable.", ghost_x)
            raw = None

        if raw is not None:
            break

        if time.monotonic() >= deadline:
            log.debug(
                "ghost:col:%d toujours absent après %.1fs — colonne morte utilisée.",
                ghost_x, timeout,
            )
            raw = "0" * cfg.height
            break

        time.sleep(poll_interval)

    height = cfg.y_end - cfg.y_start + 1
    # Sécurité : tronquer/padder si longueur inattendue
    if len(raw) != height:
        log.warning(
            "ghost:col:%d — longueur inattendue %d (attendu %d). Colonne ajustée.",
            ghost_x, len(raw), height,
        )
        raw = raw[:height].ljust(height, "0")

    return {
        (ghost_x, cfg.y_start + i): int(ch)
        for i, ch in enumerate(raw)
    }


def gather_ghost_cells(
    cfg: ZoneConfig,
    neighbour_clients: dict[str, redis.Redis],
) -> CellMap:
    """
    Collecte toutes les ghost cells nécessaires depuis les voisins.

    zone-1 : besoin de ghost_col=100  (bord gauche de zone-2)
    zone-2 : besoin de ghost_col=99   (bord droit de zone-1)
           + besoin de ghost_col=200  (bord gauche de zone-3)
    zone-3 : besoin de ghost_col=199  (bord droit de zone-2)
    """
    ghost: CellMap = {}

    if cfg.left_neighbour:
        nb = cfg.left_neighbour
        ghost.update(
            fetch_ghost_column(
                neighbour_clients[nb.zone_name],
                ghost_x=nb.their_ghost_col,   # = cfg.x_start - 1
                cfg=cfg,
            )
        )

    if cfg.right_neighbour:
        nb = cfg.right_neighbour
        ghost.update(
            fetch_ghost_column(
                neighbour_clients[nb.zone_name],
                ghost_x=nb.their_ghost_col,   # = cfg.x_end + 1
                cfg=cfg,
            )
        )

    return ghost


# ──────────────────────────────────────────────────────────────────────────────
# 5. Règles de Conway
# ──────────────────────────────────────────────────────────────────────────────

# Décalages Moore (8 voisins)
_MOORE = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if (dx, dy) != (0, 0)]


def _count_neighbours(x: int, y: int, combined: CellMap) -> int:
    """Compte les voisins vivants dans le voisinage de Moore de (x, y)."""
    count = 0
    for dx, dy in _MOORE:
        nx, ny = x + dx, y + dy
        # Les cellules hors grille sont mortes (pas de wrap)
        if 0 <= nx < GRID_WIDTH and 0 <= ny < GRID_HEIGHT:
            count += combined.get((nx, ny), 0)
    return count


def next_generation(own: CellMap, ghost: CellMap, cfg: ZoneConfig) -> CellMap:
    """
    Calcule l'état de la prochaine génération pour les cellules de cette zone.

    Règles de Conway :
      - Cellule vivante avec 2 ou 3 voisins → survit
      - Cellule vivante avec < 2 ou > 3 voisins → meurt
      - Cellule morte avec exactement 3 voisins → naît

    `ghost` contient les colonnes fantômes des zones adjacentes ; on les fusionne
    temporairement dans `combined` pour évaluer correctement les cellules de bord.
    """
    combined: CellMap = {**own, **ghost}
    result: CellMap = {}

    for x, y in cfg.iter_cells():
        alive = own.get((x, y), 0)
        n = _count_neighbours(x, y, combined)

        if alive:
            result[(x, y)] = 1 if n in (2, 3) else 0
        else:
            result[(x, y)] = 1 if n == 3 else 0

    return result


# ──────────────────────────────────────────────────────────────────────────────
# 6. Boucle principale
# ──────────────────────────────────────────────────────────────────────────────

def run() -> None:
    """
    Point d'entrée principal du worker.

    Séquence de démarrage :
      1. Résolution de la configuration de zone (GRID_ZONE + env Redis)
      2. Connexion à Redis propre + voisins (avec retry exponentiel)
      3. Seed aléatoire si la zone est vierge
      4. Boucle infinie : load → publish ghosts → fetch ghosts → compute → save

    La boucle est cadencée par TICK_INTERVAL : si le calcul est plus rapide
    que l'intervalle, on dort le temps restant. S'il est plus lent (grilles
    très denses), on enchaîne sans pause et un warning est émis.
    """

    # ── Config ────────────────────────────────────────────────────────────────
    cfg = get_zone_config()
    log.info(
        "Worker démarré — zone=%s  cols=[%d, %d]  cellules=%d  tick=%.2fs",
        cfg.zone_name, cfg.x_start, cfg.x_end, cfg.total_cells, TICK_INTERVAL,
    )

    # ── Connexion Redis propre ────────────────────────────────────────────────
    own_redis = connect_with_retry(cfg.redis_host, cfg.redis_port, label=cfg.zone_name)

    # ── Connexion Redis voisins ───────────────────────────────────────────────
    # On connecte même les voisins au démarrage pour détecter les erreurs tôt.
    neighbour_clients: dict[str, redis.Redis] = {}
    for nb in (cfg.left_neighbour, cfg.right_neighbour):
        if nb is not None:
            neighbour_clients[nb.zone_name] = connect_with_retry(
                nb.redis_host, nb.redis_port, label=nb.zone_name
            )

    log.info(
        "Voisins connectés : %s",
        list(neighbour_clients.keys()) or ["aucun (zone de bord)"],
    )

    # ── Seed initial ──────────────────────────────────────────────────────────
    if _zone_is_empty(own_redis, cfg):
        seed_zone(own_redis, cfg)
    else:
        log.info("Zone déjà initialisée — skip seed.")

    # ── Boucle de simulation ──────────────────────────────────────────────────
    generation = 0
    PAUSE_KEY = "gol:paused"

    while True:
        tick_start = time.monotonic()

        try:
            # 0. V\u00e9rifier si la simulation est en pause
            #    La cl\u00e9 `gol:paused` est \u00e9crite dans redis-1 par le backend /stop.
            #    Si pr\u00e9sente, on attend et on saute le tick courant.
            try:
                is_paused = own_redis.get(PAUSE_KEY)
            except redis.exceptions.RedisError:
                is_paused = None   # \u00e9chec Redis \u2192 on continue par s\u00e9curit\u00e9

            if is_paused:
                time.sleep(0.2)
                continue

            # 1. Charger l'\u00e9tat courant depuis Redis
            own_state = load_zone(own_redis, cfg)

            # 2. Publier nos colonnes de bord pour les voisins
            #    (fait AVANT de lire les voisins pour que tout le monde soit à jour)
            publish_ghost_columns(own_redis, cfg, own_state)

            # 3. Lire les ghost columns des voisins
            ghost_cells = gather_ghost_cells(cfg, neighbour_clients)

            # 4. Appliquer les règles de Conway
            new_state = next_generation(own_state, ghost_cells, cfg)

            # 5. Sauvegarder le nouvel état
            save_zone(own_redis, new_state)

            # 6. Signaler la fin du tick (utilisé par le backend pour la synchro)
            own_redis.set(generation_key(generation), "1", ex=30)

            # 7. Notifier le backend via Pub/Sub que la generation est prete.
            #    Convention : seul zone-1 publie sur `gol:tick` pour eviter
            #    que le backend recoive 3 notifications identiques par tick.
            if cfg.zone_name == 'zone-1':
                own_redis.publish('gol:tick', str(generation))

            # ── Métriques du tick ─────────────────────────────────────────────
            elapsed = time.monotonic() - tick_start
            alive   = sum(new_state.values())
            born    = sum(1 for (x, y), v in new_state.items() if v == 1 and own_state.get((x, y), 0) == 0)
            died    = sum(1 for (x, y), v in new_state.items() if v == 0 and own_state.get((x, y), 0) == 1)

            log.info(
                "[gen %06d]  zone=%-6s  vivantes=%-6d  (+%d/-%d)  durée=%.3fs",
                generation, cfg.zone_name, alive, born, died, elapsed,
            )

            if elapsed > TICK_INTERVAL:
                log.warning(
                    "Tick trop lent ! %.3fs > %.3fs — "
                    "envisager d'augmenter TICK_INTERVAL ou de réduire la grille.",
                    elapsed, TICK_INTERVAL,
                )

        except redis.exceptions.RedisError as exc:
            # Ne pas crasher sur une erreur Redis transitoire (reconnexion auto)
            log.error("Erreur Redis au tick %d : %s — on continue.", generation, exc)

        generation += 1

        # Attendre le temps restant pour respecter TICK_INTERVAL
        sleep_for = max(0.0, TICK_INTERVAL - (time.monotonic() - tick_start))
        time.sleep(sleep_for)


# ──────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run()
