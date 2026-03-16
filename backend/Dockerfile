# backend/Dockerfile
# ─────────────────────────────────────────────────────────────────────────────
# Build context : racine du repo (docker-compose context: ..)
# Pourquoi racine : le backend importe shared/ qui est hors de backend/
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1 : installation des dépendances ───────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Uniquement requirements.txt d'abord → cache Docker optimal
COPY backend/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2 : image de production ────────────────────────────────────────────
FROM python:3.12-slim

# Métadonnées
LABEL org.opencontainers.image.title="GOL Backend" \
      org.opencontainers.image.description="FastAPI backend – Game of Life distribué"

WORKDIR /app

# Packages installés par le builder
COPY --from=builder /install /usr/local

# Code source : shared/ + backend/
COPY shared/ ./shared/
COPY backend/  ./

# Utilisateur non-root
RUN addgroup --system appgroup \
 && adduser  --system --ingroup appgroup --no-create-home appuser
USER appuser

EXPOSE 8000

# Uvicorn : 1 worker (le seul recommandé derrière un proxy / dans un conteneur)
# --no-access-log réduit le bruit ; retirez-le si vous voulez voir les requêtes
CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--no-access-log"]
