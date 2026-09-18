# ---- Build stage ----
FROM node:20-alpine AS build

WORKDIR /app

# Install deps first so this layer is cached unless package*.json changes.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Baked into the production JS bundle at build time (Vite env vars are
# resolved at build, not runtime). Empty by default: src/api/client.js and
# src/api/events.js treat an unset VITE_API_BASE_URL as "call same-origin
# /api/...", which is what lets this image be served behind its own nginx
# reverse proxy (see nginx.conf) with no CORS involved at all — the browser
# never leaves the origin it loaded the GUI from. Override with
# --build-arg VITE_API_BASE_URL=http://some-host:8080 only for a deployment
# that does NOT put this nginx in front of sentinel-ai (e.g. pointing a
# local `npm run dev` at a port-forwarded sentinel-ai for ad hoc testing).
ARG VITE_API_BASE_URL=
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL

RUN npm run build

# ---- Serve stage ----
FROM nginx:1.27-alpine AS serve

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

# 80, not 8081: this container is the Sentinel control plane's public
# entrypoint now (see nginx.conf's module docstring), not a ClusterIP
# service reached only via kubectl port-forward.
EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1:80/healthz || exit 1

CMD ["nginx", "-g", "daemon off;"]
