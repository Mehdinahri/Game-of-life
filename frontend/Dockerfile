# frontend/Dockerfile
# Build context : game-of-life/

# ── Stage 1 : build React ─────────────────────────────────────────────────────
FROM node:20-alpine AS builder

WORKDIR /app

COPY frontend/package.json ./
RUN npm install --silent

COPY frontend/public/index.html ./public/index.html

COPY frontend/src/index.js  ./src/index.js
COPY frontend/src/index.css ./src/index.css
COPY frontend/src/App.js    ./src/App.js
COPY frontend/src/App.css   ./src/App.css

COPY frontend/src/hooks/useWebSocket.js  ./src/hooks/useWebSocket.js
COPY frontend/src/hooks/useGameCanvas.js ./src/hooks/useGameCanvas.js

COPY frontend/src/components/GameCanvas.js  ./src/components/GameCanvas.js
COPY frontend/src/components/GameCanvas.css ./src/components/GameCanvas.css
COPY frontend/src/components/Hud.js         ./src/components/Hud.js
COPY frontend/src/components/Hud.css        ./src/components/Hud.css
COPY frontend/src/components/Toast.js       ./src/components/Toast.js
COPY frontend/src/components/Toast.css      ./src/components/Toast.css

ARG REACT_APP_API_URL=http://localhost:8000
ARG REACT_APP_WS_URL=ws://localhost:8000/ws
ENV REACT_APP_API_URL=$REACT_APP_API_URL \
    REACT_APP_WS_URL=$REACT_APP_WS_URL

RUN npm run build

# ── Stage 2 : Nginx (root) ────────────────────────────────────────────────────
# On reste root pour modifier les fichiers de config nginx
FROM nginx:1.27-alpine

# Modifier le fichier nginx.conf principal pour changer le chemin du PID
# (doit être fait en root AVANT de switcher vers nginx)
RUN sed -i 's|/run/nginx.pid|/tmp/nginx.pid|' /etc/nginx/nginx.conf

# Créer les dossiers cache et donner les droits
RUN mkdir -p /var/cache/nginx/client_temp \
             /var/cache/nginx/proxy_temp \
             /var/cache/nginx/fastcgi_temp \
             /var/cache/nginx/uwsgi_temp \
             /var/cache/nginx/scgi_temp \
 && chown -R nginx:nginx \
             /var/cache/nginx \
             /var/log/nginx \
             /etc/nginx/conf.d

RUN rm /etc/nginx/conf.d/default.conf
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/build /usr/share/nginx/html
RUN chown -R nginx:nginx /usr/share/nginx/html

USER nginx
EXPOSE 80

HEALTHCHECK --interval=15s --timeout=5s --start-period=5s --retries=3 \
  CMD wget -qO- http://localhost/ || exit 1

CMD ["nginx", "-g", "daemon off;"]