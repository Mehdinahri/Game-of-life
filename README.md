# Distributed Game of Life

Une implémentation distribuée du célèbre Jeu de la Vie de Conway, utilisant une architecture de microservices avec **FastAPI**, **Redis** et **React**.

![Interface du Jeu de la Vie](https://img.shields.io/badge/Status-Active-brightgreen)
![Python](https://img.shields.io/badge/Python-3.12-blue)
![React](https://img.shields.io/badge/React-18-61dafb)
![Redis](https://img.shields.io/badge/Redis-7-red)

## 🌟 Fonctionnalités Interactive

- **Grille Distribuée (Sharding)** : La grille de 60x50 est divisée en 3 zones (A, B, C), chacune gérée par son propre worker et son instance Redis. 
- **Mode Dessin Interactif** : Mettez la simulation en pause et dessinez directement vos propres cellules sur le canvas en cliquant et glissant.
- **Synchronisation Temps Réel** : Le backend FastAPI communique avec le frontend React via WebSockets pour un rendu fluide (à 10 générations/seconde).
- **Communication Inter-Zones** : Les workers synchronisent les cellules aux frontières (ghost columns) avec leurs voisins pour assurer la continuité des règles de Conway.
- **Interface Rétro (CRT)** : Un HUD complet avec boutons de contrôle (START, STOP, CLEAR, RESET), statistiques par zone, latence et taux de rafraîchissement.

## 🏗 Architecture

Le système utilise `docker-compose` pour orchestrer les services suivants :

1. **Frontend (Nginx / React)** : Interface utilisateur avec canvas HTML5 et animations.
2. **Backend (FastAPI)** : API REST et serveur WebSocket. Gère les requêtes utilisateur (Start, Stop, Dessin) et diffuse l'état global.
3. **Workers (Python)** : 3 instances. Chaque worker est responsable du calcul (règles de Conway) pour un tiers de la grille (20 colonnes).
4. **Redis** : 3 instances. Chaque instance stocke l'état d'une zone et gère le système Pub/Sub (`gol:tick`).

## 🚀 Installation & Lancement

Prérequis : [Docker](https://docs.docker.com/get-docker/) et Docker Compose.

```bash
# Cloner le dépôt
git clone https://github.com/Mehdinahri/Game-of-life.git
cd Game-of-life

# Lancer tous les services
docker compose build --no-cache
docker compose up
```

L'application sera ensuite accessible sur : **http://localhost:3000**

## 🎮 Comment Jouer

1. Lors du lancement, la grille est initialisée aléatoirement avec environ 900 cellules.
2. Cliquez sur **■ STOP** pour mettre en pause. La grille passe en **✏ DRAW MODE**.
3. **Cliquez et glissez** sur la grille noire pour placer vos propres cellules (elles apparaîtront en cyan).
4. Cliquez sur **▶ START** pour envoyer vos cellules aux instances Redis et reprendre l'évolution.
5. Utilisez **✕ CLEAR** en mode pause pour effacer tous vos dessins.
6. Utilisez **⟳ RESET** pour remplir à nouveau la grille aléatoirement.

## 📁 Structure du Projet

```text
Game-of-life/
├── backend/          # Serveur FastAPI (WebSocket & REST API)
├── frontend/         # Application React (Canvas, HUD, Hooks)
├── worker/           # Logique de calcul distribué (Conway)
├── shared/           # Configuration commune (tailles, utils.py)
└── docker-compose.yml # Orchestration Docker
```
