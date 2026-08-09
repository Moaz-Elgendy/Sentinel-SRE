# ---- Build stage ----
FROM node:20-alpine AS build

WORKDIR /app

# Install deps first so this layer is cached unless package*.json changes.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Baked into the production JS bundle at build time (Vite env vars are
# resolved at build, not runtime). Defaults to the citizen-service port
# docker-compose already publishes to the host — this is what the
# *browser* calls, not a container-network address, since this code runs
# client-side. Override with --build-arg for a non-default deployment.
ARG VITE_API_BASE_URL=http://localhost:8000
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL

RUN npm run build

# ---- Serve stage ----
FROM nginx:1.27-alpine AS serve

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -qO- http://localhost:3000/healthz || exit 1

CMD ["nginx", "-g", "daemon off;"]
