# Digital Citizen Services Portal — Build Phases

A realistic, intentionally manageable cloud-native application that simulates a government
digital services portal. It's built to be the **sample production workload monitored by
Sentinel AI** (an autonomous SRE platform, built separately) — so it's designed from day one
for observability, health checks, and controlled failure injection.

> This is a demonstration project. It does not represent any real government system or process,
> and national IDs / documents used here are fake/generated.

This repo is being built incrementally, phase by phase. Each phase is fully working and tested
before the next one starts. This file replaces the old `Phase1.md` / `Phase2.md` / `Phase3.md` —
everything that was in them is preserved here, in one place, alongside Phases 4 and 5.

## Project status

- [x] Phase 1 — Citizen Service + PostgreSQL
- [x] Phase 2 — Notification Service
- [x] Phase 3 — React Frontend
- [x] Phase 4 — Dockerize everything
- [x] Phase 5 — Docker Compose (full stack)
- [x] Phase 6 — Health checks, metrics, structured logs, request IDs
- [x] Phase 7 — Kubernetes manifests
- [~] Phase 8 — Deploy to Kubernetes (automation written, not yet run on a real cluster — see below)
- [ ] Phase 9 — Prometheus / Grafana / Loki / Alertmanager
- [ ] Phase 10 — Chaos / failure injection
- [ ] Phase 11 — CI/CD
- [ ] Phase 12 — End-to-end incident simulations

## Architecture (current)

```text
                    React Frontend  (nginx, Phase 3/4)
                     localhost:3000
                           |
                           | HTTP/REST (JWT bearer)
                           v
                Citizen Service  ------------>  Notification Service
                  localhost:8000   fire-and-      localhost:8001
                    |                forget              |
                    v                                    v
              PostgreSQL                          PostgreSQL
           (citizen_portal)                  (notification_service)
```

All three application services (frontend, citizen-service, notification-service) plus both
Postgres instances now come up from a single `docker compose up --build` (Phases 4 and 5). The
frontend never talks to Notification Service directly — it only reflects state that Citizen
Service already exposes (a request's current status). Notification Service stays a purely
server-to-server dependency.

Each backend service owns its own database — no cross-service joins, no shared schema. Citizen
Service calls Notification Service over plain HTTP with an ID-only payload (`citizen_id`,
`request_id`), never a DB reference.

---

## Phase 1 — Citizen Service + PostgreSQL

### What's implemented

**Citizen Service** (`citizen-service/`), a FastAPI backend, with:

- **Auth** — `POST /api/auth/register`, `POST /api/auth/login`, `GET /api/auth/me`
  (JWT-based; passwords hashed with bcrypt, never stored or logged in plain text)
- **Citizen profile** — `GET /api/profile`, `PUT /api/profile`
- **Government services catalog** (read-only, seeded) — `GET /api/services`, `GET /api/services/{id}`
- **Service requests** — `POST /api/requests`, `GET /api/requests`, `GET /api/requests/{id}`,
  `PUT /api/requests/{id}`
- **Health/readiness probes** — `GET /healthz` (liveness), `GET /readyz` (checks DB connectivity)
- **Prometheus metrics** — `GET /metrics` (via `prometheus-fastapi-instrumentator`)
- **Structured JSON logging** to stdout, tagged with `service: citizen-service`, ready for Loki
- **PostgreSQL schema** managed with Alembic migrations (`citizens`, `services`, `requests`, `documents`
  tables — `documents` table exists now for forward-compatible schema; the upload endpoint itself
  is deferred to a later phase)
- **22 passing pytest tests** covering auth, profile, services, requests, and health endpoints
  (started at 20, gained 2 in Phase 2 for the notification-dispatch behavior)

### Fixed since first written

- **Postgres `Enum` value mismatch.** `RequestStatus.status` originally relied on SQLAlchemy's
  default `Enum` behavior, which sends the Python enum **member names** ("pending",
  "under_review", ...) to Postgres, while the Alembic migration creates the native Postgres enum
  type using the **string values** ("Pending", "Under Review", ...). Against SQLite (what the
  test suite uses) this mismatch is invisible because SQLite has no native enum type to enforce
  it — it only surfaces as a real `invalid input value for enum requeststatus` error against
  actual Postgres. Fixed by passing `values_callable` to the SQLAlchemy `Enum()` column so it
  sends values, not names, matching what the migration created. **Lesson for later phases:**
  the test suite's SQLite backing is convenient but doesn't catch every Postgres-specific
  behavior — worth adding a docker-compose-based integration test pass in Phase 11 (CI/CD)
  that runs the suite against real Postgres too.

### What's missing / deferred on purpose

- Document upload endpoint (schema exists, route doesn't yet)
- Kubernetes manifests, observability stack, chaos endpoints, CI/CD — all later phases
- No role/employee separation — nothing in Phase 1 lets anyone but the citizen who owns a
  request see or touch it; a caseworker/admin view would need its own auth model

### Possible improvements

- Move `required_documents` from JSON to a native Postgres `ARRAY` once SQLite-based testing is
  no longer a hard requirement (currently JSON specifically so the test suite doesn't need a live
  Postgres)
- Add pagination to `GET /api/requests` (currently returns everything for the citizen — fine at
  demo scale, not at real scale)
- Rate-limit `/api/auth/login` and `/api/auth/register`

### Tech stack

Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0, Alembic, PostgreSQL 16, JWT auth
(`python-jose`) + bcrypt (`passlib`), Prometheus instrumentation, pytest against in-memory SQLite.

---

## Phase 2 — Notification Service

### What's implemented

**Notification Service** (`notification-service/`), a second FastAPI backend:

- `POST /api/notifications` — accept and "send" a notification. There's no real email/SMS provider
  wired up (this is a demo); sending means validating the payload, logging it, and persisting the
  outcome (`Sent` or `Failed`) so the record still looks and behaves like a real delivery pipeline.
- `GET /api/notifications` — list notifications, filterable by `citizen_id` and/or `request_id`.
- `GET /api/notifications/{id}` — fetch a single notification.
- `GET /healthz`, `GET /readyz`, `GET /metrics` — same probe/metrics pattern as Citizen Service.
- **Chaos hook already in place for Phase 10.** `CHAOS_MODE=true` + `CHAOS_FAILURE_RATE` (0.0–1.0)
  makes a configurable fraction of "sends" deliberately fail with a simulated provider error, so
  Sentinel will later have a controlled way to generate a notification-delivery incident. Off by
  default; doesn't affect normal operation.
- **PostgreSQL schema** managed with its own Alembic migration (`notifications` table only — this
  service's database is intentionally minimal and independent of Citizen Service's).
- **9 passing pytest tests** covering health, create/list/get, filtering, 404s, and chaos-mode
  failure injection.

**Citizen Service changes:**

- `POST /api/requests` and `PUT /api/requests/{id}` call Notification Service
  (`app/core/notifications.py: NotificationClient`) whenever a request is submitted or its status
  actually changes.
- The call is **fire-and-forget**: it runs as a FastAPI `BackgroundTask` *after* the response is
  already sent, wrapped in a broad `httpx.HTTPError` catch. A slow or completely unavailable
  Notification Service never adds latency to, or fails, the citizen-facing request. Failures are
  logged (`notification_dispatch_failed`) so this becomes an observable symptom for Sentinel later,
  rather than a citizen-service outage.
- `NotificationClient` is injected via a FastAPI dependency (`get_notification_client`), the same
  pattern already used for `get_db`, so tests can swap in a no-op fake instead of making real network
  calls — see `tests/conftest.py: _FakeNotificationClient`.

### What's missing / deferred on purpose

- No real email/SMS provider — sends are simulated (see design notes below)
- No message queue — a downed Notification Service means notifications are silently dropped, not
  queued for retry (acceptable for the demo's purpose; see design notes)
- No admin/browsable UI for notifications yet — only reachable via its own API or logs

### Possible improvements

- Add a retry queue (or swap fire-and-forget for a lightweight broker like RabbitMQ) if Phase 12's
  incident simulations want to demonstrate queue-based resilience specifically
- Wire a real provider (SendGrid/Twilio) behind a feature flag, so `CHAOS_MODE` failures and *real*
  provider failures are both exercised
- Surface a "recent notifications" panel in the frontend once there's a sensible way to expose
  Notification Service data to the browser without calling it directly

### Tech stack

Same as Phase 1 — no new technologies, just a second service built to the same conventions.

### Design notes / assumptions

- **One database per microservice.** `notification-service` gets its own PostgreSQL instance
  (`notification-postgres`, its own volume, its own port on the host) rather than a second schema
  bolted onto Citizen Service's database. This is the realistic pattern and keeps the two services
  independently deployable/scalable — a deliberate design choice for a platform meant to simulate
  production-like failure domains for Sentinel to reason about.
- **No foreign keys across services.** `Notification.citizen_id` / `Notification.request_id` are
  plain UUID columns with no `ForeignKey` — they reference rows that live in a different database
  entirely. Referential integrity across service boundaries is the producer's responsibility
  (citizen-service only ever sends IDs it knows are valid), not something the database can enforce.
- **Fire-and-forget over synchronous call or message queue.** A message broker would be the "more
  correct" production pattern for guaranteed delivery, but it's out of scope for this phase — the
  goal here is a realistic *failure surface* (a downstream HTTP dependency that can degrade or go
  down) for Sentinel to detect and reason about, not a fully durable notification pipeline.
- **Simulated delivery, not a real provider.** No SendGrid/Twilio/etc. integration — keeps the whole
  stack runnable offline and deterministic for tests, while the chaos-mode hook still gives Sentinel
  something realistic to detect later.

---

## Phase 3 — React Frontend

### What's implemented

**`frontend/`** — a Vite + React 19 single-page app:

- **Auth** — Register and Login pages backed by `POST /api/auth/register` / `POST /api/auth/login`.
  JWT is kept in `localStorage` and attached to every request via an axios interceptor. A 401
  response from any endpoint automatically clears the local session (`src/api/client.js`).
- **Services catalog** — public `Services` page (`GET /api/services`, no auth required) showing
  each service's description, required documents, and estimated processing time, with a
  "Request this service" action that redirects to login first if the visitor isn't authenticated.
- **My Requests** — authenticated list of the citizen's own requests (`GET /api/requests`) joined
  client-side with the services catalog for display, plus a detail page per request
  (`GET /api/requests/{id}`) showing status, timeline, and any caseworker note.
- **Profile** — view/edit full name and phone (`GET`/`PUT /api/profile`); email and national ID are
  shown but intentionally read-only, matching what the backend actually allows `CitizenUpdate` to
  change.
- **Route protection** — `/requests`, `/requests/:id`, and `/profile` redirect unauthenticated
  visitors to `/login` and return them to where they were headed after logging in
  (`src/components/ProtectedRoute.jsx`).
- Loading states, inline error banners (FastAPI's `{"detail": ...}` / Pydantic validation array
  shapes are both normalized into readable text), and empty states throughout.

### Fixed since first written

- **CORS/network errors were reported as a generic, unhelpful message.** `extractErrorMessage`
  originally fell back to a bland "Could not submit that request." for *any* failure without a
  clear `detail` field — including axios errors that never got a response at all (an unreachable
  API or a CORS rejection). It now distinguishes "the API rejected this" (shows the real `detail`)
  from "the API never responded" (explicitly says the server may be unreachable or the origin may
  not be allowed, and to check the browser console) — see `src/api/client.js`. This turned an
  actual root-cause bug (see Phase 4/5 below) into something diagnosable from the UI itself instead
  of a dead end.

### What's missing / deferred on purpose

- No document upload UI (matches the backend — see Phase 1)
- No employee/admin view for managing request status — a citizen can't set their own request's
  status via the UI, on purpose (see design notes)
- No client-side data caching layer (React Query or similar) — each page fetches what it needs
  independently

### Possible improvements

- An employee/admin view for managing request status, once that has its own auth model
- Document upload once the backend endpoint exists
- Introduce a shared data-fetching/cache layer if more pages start needing the same data (the
  services list is currently fetched separately by both `ServicesPage` and `RequestsPage`)
- TypeScript, if the API surface grows enough that plain JS's lack of compile-time contract
  checking starts costing more than it saves

### Tech stack

React 19, React Router 6, Axios, Vite (dev server + production build), plain hand-written CSS (no
framework). No TypeScript in this pass — the API surface is small enough that one API module per
resource in `src/api/` keeps the request/response shapes obvious without the build overhead.

### Design notes / assumptions

- **Citizens can't set their own request status.** The backend's `PUT /api/requests/{id}` accepts
  `status` and `employee_note`, but that's a caseworker/admin action with no role separation built
  yet. The frontend's request detail page is deliberately read-only — it would be misleading to
  expose a status control a citizen shouldn't actually be able to use meaningfully.
- **Registration logs the citizen in immediately.** `POST /api/auth/register` doesn't return a
  token, so `AuthContext.register()` chains a login call right after.
- **JWT in `localStorage`, not an httpOnly cookie.** Simplest option for a demo SPA talking to a
  separate-origin API with no server-side session; an httpOnly-cookie + CSRF-token setup would be
  the hardened choice for a real deployment.

---

## Phase 4 — Dockerize everything

### What's implemented

Every service — `citizen-service`, `notification-service`, and `frontend` — now has its own
production Dockerfile:

- **`citizen-service/Dockerfile`** / **`notification-service/Dockerfile`** — `python:3.12-slim`,
  non-root `appuser`, `HEALTHCHECK` hitting `/healthz`, runs Alembic migrations then the app on
  container start.
- **`frontend/Dockerfile`** — multi-stage build: `node:20-alpine` builds the Vite production
  bundle, then `nginx:1.27-alpine` serves the static output on port 3000. `nginx.conf` handles
  client-side routing (`try_files ... /index.html` so a hard refresh on `/requests/:id` doesn't
  404) and gzips static assets. `HEALTHCHECK` hits a dedicated `/healthz` location.
- **Build-time API URL.** The frontend's `VITE_API_BASE_URL` is baked into the JS bundle at
  `docker build` time (`ARG`/`ENV` in the Dockerfile) since it's client-side code running in the
  browser, not something that can be resolved at container-runtime like a backend env var.

### What's missing / deferred on purpose

- No multi-arch builds (`linux/amd64` + `linux/arm64`) — fine for local/demo use, worth adding
  once images are actually pushed to a registry (Phase 11, CI/CD)
- No image vulnerability scanning in the build itself — planned for Phase 11

### Possible improvements

- Pin base image digests (not just tags) for fully reproducible builds
- Add a `.dockerignore`-driven build-context size check to CI once Phase 11 exists
- Multi-arch builds via `docker buildx` once images are pushed anywhere

### Tech stack

Docker, multi-stage builds, nginx (frontend serving only).

---

## Phase 5 — Docker Compose (full stack)

### What's implemented

A single root `docker-compose.yml` brings up all five containers with one command:

```text
frontend            → localhost:3000
citizen-service      → localhost:8000
notification-service → localhost:8001
postgres (citizen)   → localhost:5432
notification-postgres → localhost:5433
```

- **Dependency ordering** — `citizen-service` waits for its own Postgres to be `service_healthy`
  before running migrations; `notification-service` does the same for its own Postgres. Neither
  backend waits for the *other* backend to be healthy — only `service_started` — because a slow or
  down Notification Service must never block Citizen Service from serving traffic (consistent with
  Phase 2's fire-and-forget design). `frontend` waits for `citizen-service` to have started.
- **Two independent Postgres instances**, each with its own named volume
  (`postgres_data`, `notification_postgres_data`) and its own healthcheck (`pg_isready`).
- **`.env`-driven configuration** for both databases' credentials, the notification chaos-mode
  toggle, and — as of the CORS fix below — the frontend's build-time API URL and both services'
  allowed browser origins.

### Fixed since first written

- **CORS was hardcoded to exactly `http://localhost:3000`.** Both backends originally hardcoded
  `allow_origins=["http://localhost:3000"]` in `CORSMiddleware`. Opening the frontend via
  `127.0.0.1:3000` instead of `localhost:3000` — which happens easily depending on the machine/
  browser — sends a different `Origin` header, and the browser silently blocks every cross-origin
  request that needs a CORS preflight (any `POST`/`PUT` with a JSON body or an `Authorization`
  header). This surfaced as `POST /api/requests` failing with no usable error message client-side
  (see the Phase 3 fix above). Fixed by making `CORS_ALLOWED_ORIGINS` a comma-separated env var
  (`app/core/config.py` on both services), defaulting to both `http://localhost:3000` and
  `http://127.0.0.1:3000`, wired through `.env` and `docker-compose.yml`. Verified directly with
  real CORS preflight requests: both default origins now succeed, and an arbitrary untrusted
  origin is still correctly rejected — the fix is additive, not "make everything permissive."

### What's missing / deferred on purpose

- No reverse proxy / single entrypoint in front of all three services — each is reached on its own
  host port. A unified ingress is more of a Kubernetes-phase (7/8) concern than a Compose one.
- No named Docker network defined explicitly — relying on Compose's default project network, which
  is sufficient here since nothing requires network segmentation yet.

### Possible improvements

- If you access the app from a hostname other than `localhost`/`127.0.0.1` (a LAN IP, a
  devcontainer/Codespaces forwarded URL, a real domain), add it to `CORS_ALLOWED_ORIGINS` in `.env`
  and rebuild — this is expected to need adjusting per environment, not a bug.
- Add a `docker-compose.override.yml` example for local dev conveniences (bind-mounted source +
  hot reload) separate from the production-shaped default file.
- Health-gate `frontend`'s `depends_on` on `citizen-service` being `service_healthy` rather than
  just `service_started`, once it's clear that doesn't fight with the "come up even if a backend
  is degraded" philosophy used elsewhere.

### Tech stack

Docker Compose, `.env`-based configuration, healthchecks-driven startup ordering.

---

## Phase 6 — Health checks, metrics, structured logs, request IDs

### What's implemented

This phase hardened both backend services into production-observable processes without changing any
citizen-facing behavior. Everything added here is infrastructure that Sentinel (and the observability
stack in Phase 9) will read; no API contract changed.

#### Request ID propagation (both services)

- **`app/middleware/request_id.py`** — `RequestIDMiddleware` (`BaseHTTPMiddleware`). On every
  inbound request it reads `X-Request-ID` from the header if present, or mints a fresh UUID v4 if
  not. The value is stored in a `contextvars.ContextVar` so it is safely isolated per async task,
  and echoed back to the caller in the `X-Request-ID` response header.
- **`app/core/logging_config.py`** — `configure_logging()` now installs a custom
  `LogRecordFactory` that injects `request_id` into every log record automatically (pulled from the
  context var). Every log line — no matter which router or internal module emits it — carries the
  same `request_id` that the HTTP client sees, so a single value can be used to correlate all log
  lines for one request across both services in Loki.
- **`NotificationClient.send()`** propagates the current `request_id` as an `X-Request-ID` header
  when calling `notification-service`, so a cross-service trace can be followed by one ID even
  before Jaeger/OTEL arrives.

#### Structured JSON logging (both services)

- `configure_logging()` (called once at module-load in `main.py`) replaces the default logging
  handler with a `python-json-logger` `JsonFormatter`. Every log line is a JSON object with:
  `timestamp`, `level`, `name`, `message`, `service` (tagged per-service: `"citizen-service"` or
  `"notification-service"`), and `request_id` when inside a request context.
- The access-log middleware (`app/middleware/access_log.py`) emits one `http_request` log entry per
  non-probe request with `method`, `path`, `status_code`, and `duration_ms`. Probe paths
  (`/healthz`, `/readyz`, `/metrics`) are skipped to avoid flooding logs with Kubernetes liveness
  noise.

#### Business-level Prometheus metrics

Both services already exposed the default `prometheus-fastapi-instrumentator` metrics (HTTP latency
histograms, request counts by route). Phase 6 added hand-rolled Counters/Histograms for
business-meaningful events:

**citizen-service** (`app/core/metrics.py`):

| Metric | Type | Labels | What it tracks |
|---|---|---|---|
| `citizen_registrations_total` | Counter | — | Successful new registrations |
| `citizen_logins_total` | Counter | `result` (success/failure) | Login attempts |
| `service_requests_total` | Counter | `status` (submitted/updated) | Service request lifecycle |
| `notification_dispatches_total` | Counter | `result` (success/failure) | Fire-and-forget calls to notification-service |

**notification-service** (`app/core/metrics.py`):

| Metric | Type | Labels | What it tracks |
|---|---|---|---|
| `notification_deliveries_total` | Counter | `channel`, `result` (sent/failed) | Simulated delivery outcomes |
| `notification_delivery_duration_seconds` | Histogram | `channel` | Simulated delivery latency |

All metrics land at the existing `GET /metrics` endpoint (Prometheus text format) — nothing new to
scrape, just richer signal for Grafana dashboards and Alertmanager rules in Phase 9.

#### Enhanced health probes

**citizen-service `/readyz`** now returns a structured `checks` map:

```json
{ "status": "ready",    "checks": { "database": "up", "notification_service": "up" } }
{ "status": "degraded", "checks": { "database": "up", "notification_service": "degraded" } }
{ "status": "not_ready","checks": { "database": "down" }, "... 503" }
```

The probe pings notification-service's `/healthz` with a 1-second timeout. A reachable DB but
unreachable downstream returns `200 degraded` rather than `503` — Citizen Service can still serve
traffic even if its downstream is sick; Sentinel will detect the degraded signal and alert without
causing a false-positive outage page for the service itself.

**notification-service `/readyz`** now returns `version` and `uptime_seconds` alongside the
database check — gives Sentinel a quick sanity-check that the service actually came up after a
restart and can distinguish "not yet started" from "crashed after startup".

#### New test coverage (both services)

- **`citizen-service/tests/test_request_id.py`** (2 tests):
  - `test_request_id_injected_if_missing` — verifies a UUID is generated and echoed in the response
    header when the client sends no `X-Request-ID`.
  - `test_request_id_echoed_if_present` — verifies the client-supplied value is echoed back
    unchanged.
- **`notification-service/tests/test_request_id.py`** (2 tests): same two assertions for the
  notification service.
- Total test counts after Phase 6: **24 passing** (citizen-service), **11 passing**
  (notification-service).

### What's missing / deferred on purpose

- No distributed tracing (Jaeger / OpenTelemetry) — `X-Request-ID` propagation is the cheap,
  dependency-free equivalent for now; OTEL spans and a Jaeger sidecar are deferred to a later phase
  once the Kubernetes layer exists to host them cleanly.
- No log shipping configuration — Loki/Promtail aren't running yet (Phase 9); the structured JSON
  format is ready and waiting, but there's nothing to scrape stdout yet.
- No alerting rules yet — Prometheus will start scraping `/metrics` in Phase 9 and Alertmanager
  rules will be written then; the business metrics are just being recorded right now.

### Possible improvements

- Add a `Gauge` for the number of currently-open DB connections (SQLAlchemy pool stats) — useful
  for diagnosing connection-pool exhaustion under load.
- Replace `X-Request-ID` with a full OpenTelemetry trace context (`traceparent` header) once a
  Jaeger/Tempo collector is available — the current propagation is a subset of what W3C
  Trace Context standardizes.
- Consider emitting `WARNING`-level log entries from `readyz` when the `notification_service`
  check is `degraded`, so Loki alerts can fire on log-level rather than needing to parse the JSON
  body.

### Tech stack

No new dependencies beyond Phase 5. `python-json-logger` and `prometheus-client` were already
present; this phase wired them into both services consistently.

---

## Running the full stack locally

```bash
git clone <repository>
cd digital-citizen-portal

cp .env.example .env
# edit .env and set a real DATABASE_PASSWORD, NOTIFICATION_DATABASE_PASSWORD, and JWT_SECRET
# for anything beyond local testing

docker compose up --build
```

Once it's up:

```text
Frontend:               http://localhost:3000
Citizen Service:        http://localhost:8000
Citizen Swagger UI:     http://localhost:8000/docs
Citizen health check:   http://localhost:8000/healthz
Citizen readiness:      http://localhost:8000/readyz
Citizen metrics:        http://localhost:8000/metrics
Notification Service:   http://localhost:8001
Notification Swagger:   http://localhost:8001/docs
Notification health:    http://localhost:8001/healthz
Notification metrics:   http://localhost:8001/metrics
Citizen PostgreSQL:      localhost:5432
Notification PostgreSQL: localhost:5433
```

On startup, `citizen-service` and `notification-service` each run their own `alembic upgrade head`
independently; `citizen-service` also seeds the six predefined government services before starting
the API.

### Try it out

1. Open `http://localhost:3000`
2. Register a new citizen account
3. Browse **Services**, submit a request for one
4. Open **My Requests** to see it, click into it for the detail view
5. Check `http://localhost:8001/api/notifications` (or `docker compose logs notification-service`)
   to see the `request_submitted` notification Citizen Service dispatched in the background

## Running the test suites

Both backend services run their pytest suites against in-memory SQLite — no Docker/Postgres
required for either:

```bash
cd citizen-service
pip install -r requirements.txt
DATABASE_PASSWORD=test JWT_SECRET=test python -m pytest -v
# Expected: 22 passed

cd ../notification-service
pip install -r requirements.txt
DATABASE_PASSWORD=test python -m pytest -v
# Expected: 9 passed
```

Frontend build/lint:

```bash
cd frontend
npm install
npm run lint      # oxlint
npm run build      # production build to frontend/dist/
```

## Project structure (current)

```text
digital-citizen-portal/
├── docker-compose.yml
├── .env.example
├── README.md
├── Phases.md                    # this file
├── k8s/                          # Phase 7 — Kubernetes manifests (see k8s/README.md)
│   ├── kustomization.yaml
│   ├── kind-config.yaml
│   ├── namespace.yaml
│   ├── postgres/
│   ├── notification-postgres/
│   ├── citizen-service/
│   ├── notification-service/
│   ├── frontend/
│   └── ingress/
├── scripts/                      # Phase 8 — deploy automation (see k8s/README.md)
│   ├── deploy-kind.sh
│   ├── deploy-minikube.sh
│   ├── teardown.sh
│   └── smoke-test.sh
├── citizen-service/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── alembic.ini
│   ├── alembic/
│   │   ├── env.py
│   │   ├── script.py.mako
│   │   └── versions/0001_initial_schema.py
│   ├── app/
│   │   ├── main.py                 # FastAPI app, CORS, router wiring
│   │   ├── seed.py                  # Seeds the government services catalog
│   │   ├── core/
│   │   │   ├── config.py            # Settings from environment variables (incl. CORS_ALLOWED_ORIGINS)
│   │   │   ├── database.py          # SQLAlchemy engine/session
│   │   │   ├── security.py          # Password hashing + JWT
│   │   │   ├── deps.py              # get_current_citizen dependency
│   │   │   ├── notifications.py     # NotificationClient — fire-and-forget calls to notification-service
│   │   │   └── logging_config.py    # Structured JSON logging
│   │   ├── models/                   # SQLAlchemy models
│   │   ├── schemas/                   # Pydantic request/response models
│   │   └── routers/                   # auth, profile, services, requests, health
│   └── tests/                          # pytest suite (SQLite-backed)
├── notification-service/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── alembic.ini
│   ├── alembic/
│   │   ├── env.py
│   │   ├── script.py.mako
│   │   └── versions/0001_initial_schema.py
│   ├── app/
│   │   ├── main.py
│   │   ├── core/
│   │   │   ├── config.py             # own DATABASE_*, CHAOS_MODE, CORS_ALLOWED_ORIGINS
│   │   │   ├── database.py
│   │   │   ├── logging_config.py
│   │   │   └── sender.py              # simulated delivery + chaos-mode failure injection
│   │   ├── models/notification.py
│   │   ├── schemas/notification.py
│   │   └── routers/
│   │       ├── health.py
│   │       └── notifications.py
│   └── tests/                          # pytest suite (SQLite-backed)
└── frontend/
    ├── Dockerfile                     # multi-stage: node build → nginx serve
    ├── nginx.conf
    ├── index.html
    ├── vite.config.js
    ├── .env.example
    └── src/
        ├── main.jsx                    # entry, wraps App in BrowserRouter
        ├── App.jsx                     # route table
        ├── index.css                   # all app styling
        ├── api/
        │   ├── client.js                # axios instance, JWT injection, 401 handling, error extraction
        │   ├── auth.js
        │   ├── profile.js
        │   ├── services.js
        │   └── requests.js
        ├── context/
        │   └── AuthContext.jsx          # session state, login/register/logout
        ├── components/
        │   ├── Navbar.jsx
        │   ├── ProtectedRoute.jsx
        │   ├── StatusBadge.jsx
        │   ├── Spinner.jsx
        │   └── AlertBanner.jsx
        └── pages/
            ├── HomePage.jsx
            ├── LoginPage.jsx
            ├── RegisterPage.jsx
            ├── ServicesPage.jsx
            ├── RequestsPage.jsx
            ├── RequestDetailPage.jsx
            ├── ProfilePage.jsx
            └── NotFoundPage.jsx
```

## Troubleshooting

- **A backend container keeps restarting / can't connect to its DB** — each Postgres has a
  healthcheck and its app waits for it (`depends_on: condition: service_healthy`), but if you
  changed a `*_DATABASE_PASSWORD` in `.env` after that Postgres volume was already created,
  Postgres won't pick up the new password. Run `docker compose down -v` to reset volumes, then
  `docker compose up --build`.
- **`alembic upgrade head` fails on first boot** — usually means Postgres wasn't ready yet despite
  the healthcheck; check `docker compose logs postgres` or `docker compose logs notification-postgres`.
- **Frontend loads but every request fails / CORS error in the console** — confirm you're opening
  the app from an origin listed in `CORS_ALLOWED_ORIGINS` (`.env`; defaults to `localhost:3000`
  and `127.0.0.1:3000`). If you're on a different host/IP, add it there and rebuild
  (`docker compose build citizen-service notification-service`).
- **"My Requests" shows nothing after submitting** — submitting a request and dispatching its
  notification are decoupled (Phase 2 design); an empty requests list specifically means the
  `POST /api/requests` call itself didn't succeed — check the browser console/network tab, not
  `notification-service`'s logs.
- **Notifications never show up but requests succeed fine** — this is by design if
  `notification-service` is down or slow; check `docker compose logs citizen-service` for
  `notification_dispatch_failed` log lines, and `docker compose ps` to confirm
  `notification-service` itself is healthy.

## Phase 7 — Kubernetes manifests

### What's implemented

Plain Kubernetes YAML (no Helm yet) that mirrors `docker-compose.yml` exactly, in a `k8s/`
directory at the repo root — see `k8s/README.md` for the full breakdown, build/deploy commands,
and every design decision explained. Summary:

- **`k8s/namespace.yaml`** — everything lives in the `citizen-portal` namespace.
- **`k8s/postgres/`** and **`k8s/notification-postgres/`** — Secret, PersistentVolumeClaim,
  Deployment (`replicas: 1`, `strategy: Recreate` — these are stateful, single-instance
  databases, not horizontally scaled), Service. One Postgres per microservice, matching the
  Phase 2/5 database-per-service boundary.
- **`k8s/citizen-service/`** and **`k8s/notification-service/`** — ConfigMap (non-secret env),
  Secret (DB credentials, JWT secret), Deployment at `replicas: 2` with an `initContainer` that
  runs `alembic upgrade head` (and `python -m app.seed` for citizen-service) once per rollout
  before the app container starts, liveness on `GET /healthz`, readiness on `GET /readyz`
  (which — per Phase 6 — can report a non-503 "degraded" status without pulling the pod out of
  rotation). `citizen-service` also gets a PVC for `/data/uploads`.
- **`k8s/frontend/`** — Deployment at `replicas: 2`, both probes on nginx's `/healthz`.
- **`k8s/ingress/ingress.yaml`** — one host, `/api` → citizen-service, `/` → frontend.
  `notification-service` deliberately has **no** Ingress rule — it's server-to-server only.
- **`k8s/kustomization.yaml`** — `kubectl apply -k k8s/` applies all 21 manifests in one command.

The trickiest real design decision: the frontend bakes `VITE_API_BASE_URL` into its JS bundle at
Vite *build* time, not at container runtime. For the Ingress's same-origin routing to work, the
image has to be built with `VITE_API_BASE_URL=""` so the browser calls relative `/api/...` paths
— this is a different build than the one `docker-compose.yml` uses (which points at
`http://localhost:8000` directly). Documented in `k8s/README.md` with the exact build command.

### Fixed since first written

Nothing — this is a new phase with no prior version to fix.

### What's missing / deferred on purpose

- **No live cluster validation.** No `kubectl` binary and no cluster were available in the
  environment these manifests were built in. What *was* verified: all 21 YAML files parse, every
  `ConfigMap`/`Secret`/`PVC` reference inside a Deployment resolves to a declared object, every
  `Service` selector matches its Deployment's pod labels, namespaces are consistent, and every
  path in `kustomization.yaml` exists on disk. A real `kubectl apply --dry-run=server` (or just
  applying for real) against an actual cluster is the first thing to do in Phase 8 — treat these
  manifests as reviewed-but-not-yet-cluster-tested.
- **No actual deployment yet** — no cluster is running, nothing has been applied. That's Phase 8.
- **Plaintext demo secrets** — the `Secret` manifests use the same placeholder credentials as
  `.env.example` (`sentinal`), committed as plain YAML. Fine for a local kind/minikube demo,
  explicitly not fine for anything real — SOPS/External Secrets lands in Phase 11.
- **No HorizontalPodAutoscaler, no NetworkPolicy, no PodDisruptionBudget** — not needed yet at
  this stage; worth adding once there's real traffic/failure data to size and scope them against.
- **No Helm / Kustomize overlays for multiple environments** — intentional, see `k8s/README.md`;
  one environment (local cluster) exists right now, so overlays would be speculative complexity.

### Possible improvements

- Add a `NetworkPolicy` restricting `notification-service`'s ingress to only
  `citizen-service`'s pod selector, formalizing in-cluster what's currently just "no Ingress rule
  points at it."
- Add a `PodDisruptionBudget` for `citizen-service` and `notification-service` (`minAvailable: 1`)
  once Phase 8 exercises rolling restarts, so a voluntary disruption (node drain, `kubectl
  rollout restart`) can't take both replicas down at once.
- Move `citizen-service`'s uploads off a `ReadWriteOnce` PVC and onto object storage (or a
  `ReadWriteMany` StorageClass) before this ever runs on a multi-node cluster — see the comment
  in `k8s/citizen-service/deployment.yaml`.

### Tech stack

Kubernetes YAML manifests (`apps/v1` Deployments, `v1` Services/Secrets/ConfigMaps/PVCs,
`networking.k8s.io/v1` Ingress), Kustomize (bases only, no overlays) for the apply-everything
entrypoint. No new application dependencies — this phase is pure infrastructure-as-code sitting
on top of the images Phase 4 already builds.

---

## Phase 8 — Deploy to Kubernetes

### What's implemented

Full deployment automation, built on top of Phase 7's manifests:

- **`k8s/kind-config.yaml`** — a kind cluster config with an `ingress-ready=true` node label and
  `extraPortMappings` for host ports 80/443, so the Ingress installed inside the cluster is
  actually reachable from outside it.
- **`scripts/deploy-kind.sh`** — one script, seven steps: create the kind cluster (idempotent —
  reuses an existing one), install ingress-nginx (kind-specific manifest, pinned version, see
  the "Ingress controller note" below), build all three images, load them into kind's image
  store, `kubectl apply -k k8s/`, wait for every Deployment's rollout to complete, then print the
  `/etc/hosts` line and a smoke-test command.
- **`scripts/deploy-minikube.sh`** — the same seven-step flow for minikube (`minikube start`,
  `minikube addons enable ingress`, build, `minikube image load`, apply, wait, print next steps)
  for anyone who already has minikube set up instead of kind.
- **`scripts/teardown.sh`** — `./scripts/teardown.sh kind` or `./scripts/teardown.sh minikube`,
  a full reset (deletes the cluster, including PVCs).
- **`scripts/smoke-test.sh`** — scripts the exact "Try it out" flow from this doc against a live
  deployment: register a real citizen, log in, browse the seeded services, submit a request,
  confirm it shows up in "My Requests". Takes an optional base URL argument so it can also be
  pointed at `http://localhost:8000` to test citizen-service directly without the Ingress.

### An important, uncomfortable finding

Researching the current ingress-nginx version to pin turned up something that changes this
phase's risk profile: **the `kubernetes/ingress-nginx` project was retired by the Kubernetes
Steering and Security Response Committees in March 2026** — announced November 2025, confirmed
at retirement. No further releases, bugfixes, or security patches will ever be published for it
again. It still installs and works correctly for local kind/minikube development, which is all
this phase needs, and `scripts/deploy-kind.sh` pins the last maintained release (`v1.15.1`)
rather than something already stale. But this is now explicitly documented in
`k8s/README.md`'s "Ingress controller note" as **not safe to carry past a local demo cluster** —
before this project's Ingress layer ever faces real traffic, it needs to move to an actively
maintained alternative (Traefik, the F5 NGINX Ingress Controller, or a Gateway API
implementation). Flagging this now, while it's cheap to fix, rather than letting it become a
silent landmine for a later phase.

### What's missing / deferred on purpose

- **None of this has been run against a real cluster yet.** No `docker`, `kind`, `minikube`, or
  `kubectl` were available in the environment these scripts were written in — everything here is
  reviewed and internally consistent (every script passes `bash -n`, the ingress-nginx manifest
  URL was confirmed to resolve to a real, current release, the kind config YAML is valid) but
  **untested end-to-end**. Running `./scripts/deploy-kind.sh` for the first time for real, on an
  actual machine, is the genuinely first validation this automation gets. If it breaks on that
  first run, that's an expected part of shipping new automation, not a sign of a deeper problem
  — it just needs to be reported back so it can be fixed here.
- **No cluster-creation troubleshooting section yet** — `k8s/README.md` documents the intended
  flow, but real first-run friction (a stuck `ingress-nginx` webhook pod, a kind networking quirk
  on a particular OS, a Docker Desktop resource limit) hasn't been hit yet, so there's nothing to
  document as a fix. That section gets built from whatever actually goes wrong on first use.
- **No CI validation of the manifests** — that's Phase 11's job, once there's a pipeline to run
  `kubectl apply --dry-run=server` or a tool like `kubeconform` against every PR.

### Possible improvements

- Add a `kubeconform` (or similar schema validator) step to `scripts/deploy-kind.sh` before the
  `kubectl apply -k` step, so a manifest typo fails fast with a clear message instead of a
  confusing mid-rollout error.
- Extend `scripts/smoke-test.sh` to also verify the notification actually landed in
  `notification-service` (currently it prints manual `kubectl port-forward` instructions instead,
  since `notification-service` has no Ingress rule by design — see Phase 7).
- Once Phase 8 has been run for real at least once, capture the actual wall-clock time each step
  takes and add progress expectations to `k8s/README.md` ("ingress-nginx usually takes ~60-90s to
  become ready" etc.) so a first-time user knows what's normal versus stuck.

### Tech stack

kind and/or minikube (either works — both scripts are provided), ingress-nginx `v1.15.1` (last
maintained release — see the note above), bash for all automation scripts. No new application
dependencies.

---

## What's next

**Phase 7's manifests are written; Phase 8's deployment automation is written on top of them —
but neither has touched a real cluster yet.** That first real run is the very next step, and it
comes before any more phases are planned on top of this:

1. Run `./scripts/deploy-kind.sh` (or `deploy-minikube.sh`) for real, on a machine with Docker
   and the relevant cluster tool installed.
2. Fix whatever that first run surfaces — this is genuinely expected, not a sign something is
   wrong with the plan.
3. Run `./scripts/smoke-test.sh` and confirm it passes against the live deployment.
4. Update `k8s/README.md`'s troubleshooting section with whatever was actually encountered, so
   the next person (or the next phase) doesn't hit the same surprise blind.

Once Phase 8 is confirmed working end-to-end on a real cluster, Phases 9 through 12 continue as
planned: the full Prometheus/Grafana/Loki/Alertmanager observability stack, chaos engineering
endpoints, a CI/CD pipeline (which is also where the ingress-nginx retirement finding above
should get resolved — either by migrating the Ingress controller then, or earlier if it becomes
urgent), and finally end-to-end incident simulations for Sentinel to detect and respond to.
