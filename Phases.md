# Digital Citizen Services Portal — Build Phases

A realistic, intentionally manageable cloud-native application that simulates a government
digital services portal. It's built to be the **sample production workload monitored by
Sentinel AI**, an autonomous SRE platform — so it's designed from day one for observability,
health checks, and controlled failure injection. Through Phase 12, Sentinel was described here as
a separate platform living in its own repository; as of Phase 13 it lives in this repo, in
`sentinel-ai/`, and is deployed alongside the workload it watches.

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
- [x] Phase 8 — Deploy to Kubernetes
- [x] Phase 9 — Prometheus / Grafana / Loki / Alertmanager
- [x] Phase 10 — Chaos / failure injection
- [x] Phase 11 — CI/CD
- [x] Phase 12 — End-to-end incident simulations
- [x] Phase 13 — AWS deployment (EC2 + K3s) and Sentinel integration

Phase 13 is deliberately left **unchecked**. Every other box on this list means "written, tested,
and run for real". Phase 13's infrastructure code is written and validated — Terraform validates,
both Kustomize overlays render and pass `kubeconform`, Sentinel's lifecycle is unit-tested — but
it has never been applied: no `terraform plan` against a real account, no instance, no pod, no
incident. Checking the box would be the one dishonest line in this file.

## Architecture (current)

Local development, and the base every environment shares:

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

The AWS target added in Phase 13 — written, not yet applied:

```text
  AWS
   └── VPC / public subnet / Internet Gateway / security group (:80 only, no SSH)
        └── one t3a.large EC2 instance (Ubuntu 24.04, 40 GiB encrypted gp3)
             └── K3s v1.31.4+k3s1, single node (control-plane + worker)
                  └── Traefik (K3s's bundled ingress controller)
                       ├── frontend  ->  citizen-service  ->  notification-service
                       │                      |                      |
                       │                 citizen-postgres    notification-postgres
                       ├── Prometheus / Loki / Grafana / Alertmanager / Grafana Alloy
                       └── Sentinel AI  (FastAPI :8080, namespaced RBAC Role)
```

Everything runs on the one instance, including both databases. No EKS, no RDS, no ALB, no NAT
Gateway — see Phase 13 for why each was declined. Administration is Systems Manager Session
Manager; the Kubernetes API is not exposed publicly.

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
├── docs/
│   ├── aws-deployment.md          # Phase 13 — step-by-step AWS procedure
│   ├── github-configuration.md    # Phase 13 — GitHub Secrets vs Variables
│   └── sentinel-integration.md    # Phase 13 — Sentinel's interface contract
├── .github/
│   └── workflows/
│       └── ci-cd.yml             # Phase 13 — test, lint, validate, push to ECR, deploy via SSM
├── infra/
│   └── terraform/                 # Phase 13 — the AWS environment (validated, not applied)
│       ├── versions.tf            # provider + default tags
│       ├── variables.tf
│       ├── network.tf             # VPC, IGW, one public subnet, route table
│       ├── security.tf            # security group: :80 only, no :22, no :6443
│       ├── iam.tf                 # instance profile: SSM + ECR pull only
│       ├── ecr.tf                 # four repositories
│       ├── ec2.tf                 # the single t3a.large K3s node
│       ├── github_oidc.tf         # GitHub Actions OIDC role (no access keys)
│       ├── outputs.tf
│       ├── user_data.sh.tftpl     # K3s install, ECR credential timer, deploy helper
│       └── terraform.tfvars.example
├── k8s/                          # Phase 7/13 — Kubernetes manifests (see k8s/README.md)
│   ├── kustomization.yaml         # local entrypoint: `kubectl apply -k k8s/`
│   ├── kind-config.yaml
│   ├── base/                      # shared by both environments, no Secrets
│   │   ├── kustomization.yaml
│   │   ├── namespace.yaml
│   │   ├── postgres/
│   │   ├── notification-postgres/
│   │   ├── citizen-service/
│   │   ├── notification-service/
│   │   ├── frontend/
│   │   ├── ingress/               # ingress-nginx (local flavour)
│   │   └── monitoring/            # Phase 9 — observability stack
│   │       ├── prometheus/
│   │       ├── alertmanager/
│   │       ├── loki/
│   │       ├── alloy/
│   │       └── grafana/
│   └── overlays/
│       ├── local/                 # kind / minikube / Docker Desktop
│       │   └── secrets/           # the committed demo Secrets live here only
│       └── aws/                   # EC2 + K3s: Traefik, ECR images, Sentinel
│           ├── kustomization.yaml
│           ├── patch-*.yaml       # Traefik, ECR pull, PVCs, replicas, monitoring
│           ├── secrets/           # *.env gitignored; only *.env.example tracked
│           └── sentinel/          # Sentinel Deployment, Service, RBAC, config
├── sentinel-ai/                   # Phase 13 — the autonomous SRE agent
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── app/
│   │   ├── main.py
│   │   ├── clients/               # prometheus, loki, kubernetes, github, slack, chaos
│   │   ├── lifecycle/             # detection -> ... -> learning, incl. policy + remediation
│   │   ├── models/
│   │   ├── store/                 # SQLite incident history
│   │   └── routers/               # health, alerts webhook, incidents
│   └── tests/                     # policy, decision, allow-list, validation, RCA
├── scripts/                      # Phase 8/13 — deploy automation (see k8s/README.md)
│   ├── deploy-docker-desktop.sh
│   ├── deploy-kind.sh
│   ├── deploy-minikube.sh
│   ├── deploy-aws.sh              # Phase 13 — runs on the EC2 node
│   ├── generate-aws-secrets.sh    # Phase 13 — creates the gitignored secret files
│   ├── incident-scenarios.sh      # Phase 10/12/13 — nine chaos scenarios
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

Deployment automation, built on top of Phase 7's manifests, plus a real deployment confirmed
working on Docker Desktop's built-in Kubernetes:

- **`scripts/deploy-docker-desktop.sh`** — targets Docker Desktop's Kubernetes specifically,
  which turned out to be the simplest path for this project: the cluster is already running, and
  it shares Docker Desktop's own image store directly, so `docker build` output is immediately
  visible to the cluster with no `kind load` / `minikube image load` equivalent needed at all. It
  also natively supports `LoadBalancer` Services bound to `localhost`, so ingress-nginx needs no
  NodePort/extraPortMappings workaround. Refuses to run unless `kubectl`'s current context is
  literally `docker-desktop`, so it can never accidentally apply to the wrong cluster.
- **`k8s/kind-config.yaml`** + **`scripts/deploy-kind.sh`** — the kind-specific path (a
  cluster-in-a-container, useful if you don't use Docker Desktop's own Kubernetes or want
  something fully disposable): create cluster, install ingress-nginx (kind-specific manifest),
  build images, load them into kind's image store, apply, wait for rollout.
- **`scripts/deploy-minikube.sh`** — the equivalent for minikube.
- **`scripts/teardown.sh`** and **`scripts/smoke-test.sh`** (registers a real citizen, logs in,
  browses seeded services, submits a request, confirms it shows up — a real end-to-end check
  against a live deployment, not just "pods are Running").

### Confirmed working on a real cluster

Unlike Phase 7, this phase has now actually been run: all 8 pods (`citizen-postgres`,
`citizen-service` ×2, `frontend` ×2, `notification-postgres`, `notification-service` ×2) reached
`Running` on Docker Desktop's Kubernetes, using plain `docker build -t <name>:latest .` images
(no registry namespace) built directly from this repo's Dockerfiles. Two real things came out of
that first run, both now fixed:

1. **Image naming.** The manifests originally referenced `citizen-portal/citizen-service:latest`
   etc. (a namespaced tag, from Phase 7's original — untested — assumptions). A straightforward
   local `docker build -t citizen-service:latest ./citizen-service` doesn't produce that
   namespace. Fixed by dropping the `citizen-portal/` prefix from every `image:` field across all
   three Deployments (and updating every doc that referenced the old names) — the manifests now
   match a plain local build exactly, which is what most people will actually run.
2. **No Ingress applied yet in that first pass** — `frontend` was reached via
   `kubectl port-forward svc/frontend 3000:3000`, which loads the page but breaks all API calls:
   the frontend's JS bundle calls relative `/api/...` paths (built with `VITE_API_BASE_URL=""`,
   see Phase 7's design decisions), expecting an Ingress in front of it to route those calls to
   `citizen-service`. Port-forwarding the frontend alone has nothing to route `/api/...` to. This
   is now the first item in `k8s/README.md`'s new **Troubleshooting** section, along with the
   related (and also real) question of why pods don't show up in Docker Desktop's container GUI
   (expected — Docker Desktop's Kubernetes runs pods via `containerd`/`kubelet`, a separate
   runtime path from the one the GUI's Containers tab reads). The actual fix: apply
   `k8s/ingress/ingress.yaml` (or just run `deploy-docker-desktop.sh`, which does it
   automatically) and hit `http://citizen-portal.local` — no port-forwarding needed at all.

### An important, uncomfortable finding

Researching which ingress-nginx version to pin turned up something that changes this phase's
risk profile: **the `kubernetes/ingress-nginx` project was retired by the Kubernetes Steering and
Security Response Committees in March 2026** — announced November 2025, confirmed at retirement.
No further releases, bugfixes, or security patches will ever be published for it again. It still
installs and works correctly for local development, which is all this phase needs, and every
deploy script pins the last maintained release (`v1.15.1`) rather than something already stale.
But this is documented in `k8s/README.md`'s "Ingress controller note" as **not safe to carry past
a local demo cluster** — before this project's Ingress layer ever faces real traffic, it needs to
move to an actively maintained alternative (Traefik, the F5 NGINX Ingress Controller, or a
Gateway API implementation). Flagging this now, while it's cheap to fix, rather than letting it
become a silent landmine for a later phase.

### What's missing / deferred on purpose

- **Not yet re-verified with the Ingress actually in place end to end.** The pod-level deployment
  and the image-naming fix are confirmed; a full `smoke-test.sh` run against
  `http://citizen-portal.local` (post-Ingress-fix) hasn't been reported back yet. Worth doing as
  the very next step before treating Phase 8 as fully closed.
- **kind and minikube paths are still unverified against a real cluster** — only the Docker
  Desktop path has been run for real so far. They're written the same way and reviewed the same
  way `deploy-docker-desktop.sh` was before its first run, so the same "expect first-run friction"
  caveat applies if/when someone runs them.
- **No CI validation of the manifests** — that's Phase 11's job, once there's a pipeline to run
  `kubectl apply --dry-run=server` or a tool like `kubeconform` against every PR.

### Possible improvements

- Add a `kubeconform` (or similar schema validator) step to the deploy scripts before the
  `kubectl apply -k` step, so a manifest typo fails fast with a clear message instead of a
  confusing mid-rollout error.
- Extend `scripts/smoke-test.sh` to also verify the notification actually landed in
  `notification-service` (currently it prints manual `kubectl port-forward` instructions instead,
  since `notification-service` has no Ingress rule by design — see Phase 7).
- Capture actual wall-clock timings for each step (now that real runs exist) and add progress
  expectations to `k8s/README.md` ("ingress-nginx usually takes ~60-90s to become ready" etc.) so
  a first-time user knows what's normal versus stuck.

### Tech stack

Docker Desktop's built-in Kubernetes (primary, confirmed working), kind and minikube (alternative
paths, scripts written but not yet run for real), ingress-nginx `v1.15.1` (last maintained
release — see the note above), bash for all automation scripts. No new application dependencies.

---

## Phase 9 — Prometheus / Grafana / Loki / Alertmanager

### What's implemented

The full observability stack, in `k8s/monitoring/`, following the same plain-YAML-per-component
pattern Phase 7 established (no Helm, no Prometheus Operator CRDs):

- **Prometheus** (`monitoring/prometheus/`) — scrapes `citizen-service` and
  `notification-service` via annotation-based discovery: both Deployments' pod templates now
  carry `prometheus.io/scrape`, `prometheus.io/port`, `prometheus.io/path` annotations, and
  Prometheus's `kubernetes_sd_configs` (role: `pod`) matches on exactly those. A scoped `Role`
  (not a cluster-wide `ClusterRole`) grants just enough RBAC for pod discovery within
  `citizen-portal`. A first, deliberately small alerting rule set — `ServiceDown` (via the
  synthetic `up` metric, no application code needed), `HighHTTPErrorRate`, and
  `HighRequestLatency` (both built on `http_requests_total` / `http_request_duration_seconds`,
  already emitted since Phase 1/6's `prometheus-fastapi-instrumentator` setup).
- **Grafana** (`monitoring/grafana/`) — Prometheus and Loki datasources provisioned via
  ConfigMap (no manual "Add data source" click-through), plus one starter dashboard
  ("Citizen Portal Overview") showing pods-up, request rate, 5xx error rate, p95 latency, and a
  live log panel — deliberately the same signals the alerting rules watch, so what you see on
  the dashboard is what pages you.
- **Loki** (`monitoring/loki/`) — single-binary mode, filesystem storage on an `emptyDir` (same
  ephemeral-storage trade-off Prometheus makes — fine for a local demo, add a PVC before relying
  on real retention).
- **Alertmanager** (`monitoring/alertmanager/`) — wired to receive alerts from Prometheus. The
  routing tree's default receiver has no integrations configured yet (see "What's missing"
  below) — alerts are visible in Alertmanager's UI/API but nothing pages out yet.
- Log shipping via **Grafana Alloy**, not Promtail (see the finding below) —
  `monitoring/alloy/` is a DaemonSet using `loki.source.kubernetes` (Kubernetes API-based
  tailing) to ship every `citizen-portal` pod's logs to Loki, parsing the structured JSON logs
  Phase 6 already emits (`level`, `request_id`, `service`) into queryable Loki labels.
- `k8s/README.md` gained an "Accessing the observability stack" section (everything here is
  `kubectl port-forward`-only, deliberately not Ingress-exposed — these are internal tools, not
  citizen-facing) and a "How scraping works" explainer.
- All three deploy scripts (`deploy-docker-desktop.sh`, `deploy-kind.sh`, `deploy-minikube.sh`)
  now wait on the new Deployments and the Alloy DaemonSet's rollout, and print a pointer to the
  port-forward instructions once everything's up.

### An important finding, same shape as Phase 8's

While deciding how to ship logs to Loki, the obvious default — Promtail — turned out to already
be **deprecated**: its functionality has been merged into Grafana Alloy. Building this phase on
Promtail would have meant starting on a tool already headed for removal. `monitoring/alloy/`
uses Alloy's River-syntax config from the start instead. As a side benefit (not the reason for
the choice, but a nice one), Alloy's `loki.source.kubernetes` tails via the Kubernetes API rather
than reading host log files, so this DaemonSet needs no `hostPath` mounts or elevated privileges
the way a classic Promtail setup would.

### What's missing / deferred on purpose

- **No real Alertmanager receiver wired up.** The default receiver has zero integrations — no
  Slack webhook, no PagerDuty, no email. This is deliberate: faking a receiver with a placeholder
  webhook URL would just silently fail on every alert, which is worse than being honest that this
  decision (which channel? whose on-call?) hasn't been made yet. Alerts are inspectable via
  Alertmanager's UI/API in the meantime.
- **Not yet run against a real cluster.** Like Phase 7's original manifests, everything in
  `k8s/monitoring/` was built and internally validated (all YAML parses, every
  ConfigMap/Secret/PVC/ServiceAccount reference resolves, every Service selector matches its
  Deployment/DaemonSet's pod labels, the dashboard JSON is valid, all scripts pass `bash -n`) but
  has not yet been applied to a live cluster. Unlike Phase 8's first pass, there's no confirmed
  real-cluster run to report on yet for this phase specifically — that's the next step.
- **No readiness-degraded-vs-down alert.** Phase 6 built `/readyz` to distinguish "down" from
  "degraded" (e.g., notification-service unreachable but citizen-service still functional) in the
  JSON response body — but that distinction isn't yet exposed as a Prometheus metric, so
  Alertmanager can't currently alert on "degraded" specifically, only on a scrape actually
  failing. Needs a new gauge metric in Phase 6's health router before this is alertable.
- **No Postgres-specific alerting.** Alerting on a Postgres pod going down currently relies on
  Kubernetes' own pod-readiness safety net, not a Prometheus rule — `postgres_exporter` isn't
  deployed, so there's no Postgres-level metric to write a rule against yet.
- **No CI validation of the manifests** — still Phase 11's job, same as noted in Phase 8.

### Possible improvements

- Deploy `postgres_exporter` alongside both Postgres instances and add a `PostgresDown` /
  `PostgresReplicationLag`-style rule once there's a real metric to alert on.
- Expose the readiness-degraded state as a Prometheus gauge (e.g.
  `citizen_service_readiness_status{status="degraded"}`) so Alertmanager can distinguish
  "notification-service is unreachable but citizen-service still works" from a full outage,
  instead of only seeing the binary `up`/`down` signal.
- Wire a real Alertmanager receiver once there's an actual channel to send to.
- Move to the Prometheus Operator's `ServiceMonitor`/`PodMonitor` CRDs once there's a reason to
  manage more than two scrape targets — annotation-based discovery is the right amount of
  complexity for two services, not necessarily for more.
- Add a PVC for Prometheus's and Loki's storage (matching the pattern already used for both
  Postgres instances) before treating either's data as anything but disposable.

### Tech stack

Prometheus `v3.13.2`, Grafana `13.1.3`, Loki `3.7.5`, Grafana Alloy `v1.16.3` (not Promtail — see
the finding above), Alertmanager `v0.33.1`. All versions confirmed current via search rather than
assumed from training data, consistent with how ingress-nginx's version was handled in Phase 8.

---

## Phase 10 — Chaos engineering / failure injection

### What's implemented

Chaos is deliberately **off by default** and is controlled at runtime only when
`CHAOS_MODE=true` and a separate `CHAOS_ADMIN_TOKEN` is configured. The control surface is
intentionally kept out of the frontend; Sentinel or an operator can drive it through the backend
API.

**Citizen Service** exposes:

- `GET /api/chaos/status` — inspect the current fault state
- `POST /api/chaos/fault` — configure artificial latency, forced HTTP 5xx probability, and
  simulated database failure
- `POST /api/chaos/reset` — clear all active faults

**Notification Service** exposes the same endpoints and additionally controls
`notification_failure_rate`, which drives the existing simulated email/SMS provider failure
path. The older `CHAOS_FAILURE_RATE` environment variable remains supported as the initial
provider-failure rate for backwards-compatible Docker/Kubernetes startup configuration.

All chaos control requests require the `X-Chaos-Token` header. Missing/incorrect tokens return
404, so the existence of the control surface is not advertised to unauthenticated callers. The
control endpoints, health probes, OpenAPI endpoints, and Prometheus `/metrics` are excluded from
fault injection. Artificial latency is therefore observable without making Kubernetes believe the
pod is unhealthy, while database failure deliberately makes `/readyz` return HTTP 503 and normal
application requests return HTTP 503.

The services export Prometheus telemetry for the active fault configuration and every injected
fault: `chaos_latency_ms`, `chaos_error_rate`, `chaos_db_failure`,
`chaos_notification_failure_rate` (notification service), and
`chaos_injections_total{fault_type=...}`. Phase 9's Prometheus rules now include explicit
chaos-observation alerts plus a notification-delivery failure-rate alert, and the Grafana
overview has panels for active chaos configuration, injection events, and notification failures.

### Test scenarios

The test suites now cover authentication of the chaos control API, fault configuration/reset,
forced 5xx responses, latency injection, simulated DB failure/readiness degradation, and
notification delivery failure injection (29 tests in citizen-service, 16 in notification-service
— both suites confirmed passing). The normal notification chaos test was updated to drive the
new runtime controller while retaining the existing failure-rate behavior.

Example local control flow when running Docker Compose:

```bash
# Enable the control plane in .env first:
CHAOS_MODE=true
CHAOS_ADMIN_TOKEN=use-a-strong-random-token

# Then configure a 100% HTTP failure rate:
curl -H "X-Chaos-Token: use-a-strong-random-token" \
  -H "Content-Type: application/json" \
  -d '{"error_rate":1.0}' \
  http://localhost:8000/api/chaos/fault

# Reset afterward:
curl -X POST \
  -H "X-Chaos-Token: use-a-strong-random-token" \
  http://localhost:8000/api/chaos/reset
```

For Kubernetes, replace the demo `CHAOS_ADMIN_TOKEN` value in both service Secrets before
enabling `CHAOS_MODE`. Because each deployment has two replicas and chaos state is intentionally
in-memory, setting a fault through one pod affects that pod only. This is useful for
demonstrating partial failures; target a specific pod with `kubectl port-forward` when you need
deterministic single-pod experiments.

### What's missing / deferred on purpose

- **Phase 9's observability stack, applied against this Phase 10 code, hasn't been confirmed on
  a real cluster yet** — see the Phase 9 section's own "not yet run against a real cluster" note;
  that first real run (deploy, trigger `ServiceDown`, watch it appear in Alertmanager) still
  applies, now with chaos fault injection as an even better way to trigger it deliberately rather
  than by scaling a Deployment to 0.
- No chaos scenario library yet (a documented set of "here's what a database outage looks like on
  the dashboard" walkthroughs) — that's really Phase 12's job, once there's a reason to script a
  full incident narrative rather than a single curl command.

### Tech stack

No new dependencies — chaos state lives in-memory in each service's existing FastAPI process,
using the same Prometheus client library and structured logging already in place since Phase 6.

---

## Phase 11 — CI/CD

### What's implemented

A single GitHub Actions workflow, `.github/workflows/ci-cd.yml`, with five jobs:

- **`test-citizen-service`** / **`test-notification-service`** — the same pytest suites run
  locally throughout this project (29 and 16 tests respectively), against the same in-memory
  SQLite setup `tests/conftest.py` already uses — no live Postgres needed in CI either.
- **`lint-and-build-frontend`** — `npm run lint` (oxlint) and `npm run build`, with
  `VITE_API_BASE_URL=""` — matching the k8s-targeted build, not docker-compose's.
- **`validate-k8s-manifests`** — everything the manual Python validation script did in Phases 7–9
  (YAML parses, every `ConfigMap`/`Secret`/`PVC`/`ServiceAccount` reference resolves, every
  `Service` selector matches its `Deployment`/`DaemonSet`'s pod labels), **plus** something none
  of those phases could actually do: a real `kubeconform` schema check of `kubectl kustomize
  k8s/`'s rendered output against the genuine Kubernetes API schemas. Every previous phase's
  manifest "validation" was structural-consistency-only, because the sandbox those phases were
  built in had no cluster, no `kubectl`, and no internet access to fetch schemas with. GitHub's
  runners have both — this is the first time these manifests get checked against what Kubernetes
  itself would actually accept.
- **`build-and-push-images`** — only on a push to `main`, only after all four jobs above pass:
  builds and pushes `citizen-service`, `notification-service`, and `frontend` to GHCR
  (`ghcr.io/<owner>/citizen-portal-<service>`), tagged with both `latest` and the commit SHA.
  Uses `GITHUB_TOKEN` (no external registry credentials or cloud secrets to configure).

### An important finding, third in this series

Researching which image-scanning Action to wire in surfaced something serious enough to change
the plan: **`aquasecurity/trivy-action` — the default choice for this — was compromised twice in
2026.** A full repository takeover in late February, and, separately, a credential-stealing
supply-chain attack in March that affected *every* published tag from `0.0.1` through `0.34.2`,
exfiltrating CI secrets (which, in this pipeline, would include the `GITHUB_TOKEN` used to push
to GHCR) to an attacker-controlled domain. Clean tags exist post-incident, but wiring a
secret-bearing CI job to a third-party Action with that specific history, on the strength of "the
new tags are probably fine now," isn't a call worth making silently. Image vulnerability scanning
was left out of this phase entirely rather than added on that basis — see "What's missing" below
for what to evaluate before adding it back. This is the third time in three phases that version
research has surfaced something that changed the plan (ingress-nginx's retirement in Phase 8,
Promtail's deprecation in Phase 9) — worth noting as a pattern: verifying current tooling status
before adopting it has caught something real every single time it's been done in this project.

### What's missing / deferred on purpose

- **No automated deployment.** This pipeline builds, tests, validates, and publishes versioned
  images — it does not deploy them anywhere. There is no standing cloud cluster for this project
  to deploy to; every deployment so far has been a deliberate local step
  (`scripts/deploy-docker-desktop.sh` / `deploy-kind.sh` / `deploy-minikube.sh`), and that
  remains true after this phase. If a real hosting target gets chosen later, this workflow is the
  natural place to add a `deploy` job — until then, treating "push a versioned image to GHCR" as
  the actual CD deliverable is honest about what this project currently has to deploy *to*.
- **No image vulnerability scanning** — see the finding above. Before adding it back: prefer
  pinning any scanning Action by commit SHA rather than a tag (tags can be force-moved after
  compromise the way trivy-action's were), and evaluate current alternatives (Docker Scout,
  Anchore Grype, or GitHub's own Dependabot/code-scanning) rather than defaulting back to the
  same tool without re-checking its status at that time.
- **No real secret management.** The k8s `Secret` manifests still hold the same demo-only
  placeholder values used since Phase 7/8 (`sentinal`, `replace_me`) — this pipeline doesn't
  touch them, and neither SOPS nor External Secrets got wired in. This depends on a decision this
  project hasn't made yet: what actually holds the real secrets in a non-local deployment. Worth
  revisiting once that's decided, not before.
- **ingress-nginx migration still not done.** Flagged as deferred-to-Phase-11 back in Phase 8's
  writeup; it didn't happen here either. The reasoning holds either way: it's still fine for
  local kind/minikube/Docker Desktop use, and still not something to carry into a real
  deployment target without addressing first. Pushed forward again rather than done reflexively
  just because a phase number said to.
- **No branch protection / required-checks configuration documented.** The workflow runs on every
  push and PR to `main`, but nothing in this repo enforces that these checks must pass before a
  PR merges — that's a GitHub repository setting, not something a workflow file can configure on
  its own, and hasn't been set.

### Possible improvements

- Add a `deploy` job once a real target exists, gated on `build-and-push-images` succeeding and
  probably on a manual approval step (GitHub Environments support this natively) rather than
  auto-deploying every merge to `main`.
- Add Dependabot (or Renovate) configuration to keep the pinned versions in this workflow, the
  Dockerfiles, and `k8s/monitoring/`'s images from silently drifting stale the way ingress-nginx
  and Promtail did — this project has now hit that exact problem twice.
- Revisit image scanning with a tool whose supply-chain trust has actually been checked at
  decision time, not carried over from this write-up.
- Multi-arch image builds (`linux/amd64,linux/arm64`) via `docker/setup-qemu-action` if this ever
  needs to run on Apple Silicon or ARM-based cloud instances — not needed for local
  kind/minikube/Docker Desktop use today.

### Tech stack

GitHub Actions, GHCR (GitHub Container Registry), `kubeconform` for real Kubernetes schema
validation. No new application dependencies. Action versions used —
`actions/checkout@v7`, `actions/setup-python@v7`, `actions/setup-node@v6`,
`docker/setup-buildx-action@v4`, `docker/login-action@v4`, `docker/build-push-action@v7` — were
all confirmed current via search rather than assumed, consistent with how every version decision
has been handled since Phase 8's ingress-nginx finding.

---

## Phase 12 — End-to-end incident simulations

### What's implemented

`scripts/incident-scenarios.sh` — the closing piece that ties every previous phase together. It
drives five named, repeatable incidents through Phase 10's chaos control API (plus one that
doesn't need it) and confirms Phase 9's stack actually catches each one for real: the right
Prometheus alert reaches `firing` within its `for:` window, and — best-effort, since
Alertmanager polls on its own interval — that it also reached Alertmanager.

| Scenario | Trigger | Alert(s) confirmed | `for:` |
|---|---|---|---|
| `db-outage` | `POST /api/chaos/fault {"db_failure": true}` | `ChaosDatabaseFailure` | 30s |
| `http-errors` | `{"error_rate": 1.0}` | `ChaosForcedHTTPFailures` → `HighHTTPErrorRate` | 30s → 5m |
| `latency` | `{"latency_ms": 1500}` + generated traffic | `ChaosLatencyInjection` → `HighRequestLatency` | 30s → 5m |
| `notification-degradation` | notification-service `{"notification_failure_rate": 1.0}` + generated traffic | `NotificationDeliveryFailureRateHigh` | 5m |
| `full-outage` | `kubectl scale citizen-service --replicas=0` | `ServiceDown` | 2m |

Each scenario:

- **Pins the target Deployment to 1 replica first.** Chaos state is per-pod and in-memory
  (Phase 10's own design), so a fault set through one pod's port-forward would otherwise only
  affect whichever pod happened to receive that request — not a reliable thing to assert on with
  two replicas. The script scales back to the original replica count afterward, even on failure
  (`trap cleanup EXIT`).
- **Generates real traffic where the alert needs it.** `HighRequestLatency` and
  `NotificationDeliveryFailureRateHigh` are built on histograms/counters that only have data if
  requests actually happen — injecting the fault alone produces zero observations, not a
  passing or failing alert, just silence. The script reuses the same
  register → login → browse → submit flow `scripts/smoke-test.sh` already established, looped for
  the scenario's duration.
- **Always resets the fault it injected** (`POST /api/chaos/reset`), whether the scenario passed,
  failed, or the script was interrupted.

Run one scenario or all of them:

```bash
export CHAOS_ADMIN_TOKEN=<the same token set in both services' Secrets>
./scripts/incident-scenarios.sh db-outage
./scripts/incident-scenarios.sh all
```

### What's missing / deferred on purpose

- **Not yet run against a real cluster.** Same honest caveat as Phases 7–9 carried before their
  first real runs: this script has been validated the way everything else here has been before a
  live cluster existed to test it against — `bash -n` passes, every chaos-API field name was
  cross-checked directly against `citizen-service/app/routers/chaos.py` and
  `notification-service/app/routers/chaos.py`, every alert name and `for:` duration was
  cross-checked directly against `k8s/monitoring/prometheus/rules-configmap.yaml` rather than
  assumed. It has not yet actually been executed end-to-end on a live deployment. That first real
  run — deploy, run `./scripts/incident-scenarios.sh all`, watch every alert actually fire — is
  the natural next step, the same way it was flagged (and later closed) for Phase 8's manifests.
- **No Grafana-side verification.** The script confirms alerts fire via the Prometheus/
  Alertmanager APIs directly; it doesn't screenshot or programmatically assert on the Grafana
  dashboard panels reacting the same way. Worth doing manually once the stack is running for
  real — watching the "Citizen Portal Overview" dashboard's error-rate and latency panels move
  in response to each scenario is a big part of the point of Phase 9 existing at all.
- **No Postgres-outage scenario.** `ChaosDatabaseFailure` simulates the *application* believing
  its DB is down (Phase 10's `db_failure` flag short-circuits the app layer); it doesn't actually
  take Postgres itself down. A real Postgres-pod-killed scenario needs `postgres_exporter` and a
  Postgres-specific alert first — both flagged as missing back in Phase 9.
- **Single-cluster, single-namespace only.** No cross-region or multi-cluster failure scenario —
  out of scope for what this project's current infrastructure could even simulate.

### Possible improvements

- Add the Postgres-outage scenario once `postgres_exporter` + a `PostgresDown` rule exist (Phase 9's own "possible improvements").
- Feed this script's pass/fail output into the CI/CD workflow (Phase 11) as a scheduled job
  against a long-lived staging cluster, if one ever exists — turning it from a manual
  verification tool into a continuously-running synthetic-incident suite.
- This is exactly the script Sentinel's own test harness should be able to run against
  itself: trigger a scenario, then check whether Sentinel detected, diagnosed, and reported it
  correctly — see the Sentinel integration doc below for how that loop is meant to work once
  Sentinel has something real to watch.

### Tech stack

No new dependencies — bash, `kubectl`, `curl`, and the Prometheus/Alertmanager HTTP APIs already
running since Phase 9.

---
## Phase 13 — AWS deployment (EC2 + K3s) and Sentinel integration

The phase that closes the two loose ends Phase 12 left open — "there is no real cluster to run
any of this against" and "Sentinel lives somewhere else and this repo only describes a contract
for it". Both are now addressed in-repo: `infra/terraform/` stands up a single-node Kubernetes
cluster on AWS, and `sentinel-ai/` is Sentinel itself, running as a workload inside that cluster
next to the application it watches.

**Nothing in this phase has been applied to AWS.** The Terraform has never been planned against a
real account, no pod has ever started on the instance, and Sentinel has never run against a
cluster. That is why the checklist entry at the top of this file is unchecked. Everything below
describes code that exists and has been validated the way earlier phases validated things before
their first real run — see "What's missing" for exactly where the line falls.

### What's implemented

**Infrastructure — `infra/terraform/`, one EC2 instance running K3s.**

A deliberately small AWS footprint: a VPC (`10.20.0.0/16`) with one public subnet
(`10.20.1.0/24`), an Internet Gateway, a route table, one security group, one `t3a.large` EC2
instance (2 vCPU / 8 GiB) on Canonical's official Ubuntu 24.04 AMI, a 40 GiB encrypted gp3 root
volume, an IAM role and instance profile, and four ECR repositories. That is the whole list.

The services this project does **not** use are as much of the design as the ones it does:

- **No EKS.** A managed control plane is a standing hourly charge that accrues before a single
  workload node exists, and this project's entire compute need fits on one burstable instance.
  K3s gives a conformant Kubernetes API on that instance for the cost of the instance. The
  tradeoff is real and stated rather than hidden: there is no managed control plane, no control
  plane HA, and no managed upgrade path. For a demo whose purpose is to have a cluster Sentinel
  can act on, that is the right trade; for anything with users, it is not.
- **No RDS.** Both Postgres instances stay inside Kubernetes, exactly as they are locally. This
  one is not primarily about cost: **database failure is one of Sentinel's incident scenarios.**
  Moving Postgres to a managed service would put it outside the cluster Sentinel observes and
  outside the RBAC boundary that makes Sentinel's behaviour analysable, and would quietly delete
  the most interesting escalation case in the project — the one where the correct autonomous
  action is *no action*.
- **No ALB.** K3s ships Traefik as its bundled ingress controller, and it is reached directly on
  the instance's public IP on port 80. An ALB would add a second standing charge to route traffic
  to exactly one target.
- **No NAT Gateway.** The single subnet is public, so the instance reaches ECR, SSM and
  `get.k3s.io` through the Internet Gateway using its own public IP. A NAT Gateway exists to give
  private-subnet workloads egress; with one node that has to be reachable anyway, it buys nothing
  and costs continuously.
- **No ingress-nginx on AWS.** The retirement finding from Phase 8 has been carried forward,
  unresolved, through Phases 9, 10 and 11. It is resolved here, for the environment where it
  actually mattered: AWS uses Traefik, which K3s installs anyway. Local development still uses
  ingress-nginx, which is still fine for a laptop cluster and still not something to put in front
  of real traffic.

**No SSH, at all.** There is no port 22 ingress rule and no EC2 key pair in the Terraform.
Administration is AWS Systems Manager Session Manager, which reaches the instance through the SSM
agent's outbound connection rather than an inbound listener. The Kubernetes API on `:6443` is not
exposed publicly either. Port 80 is the only thing open (443 exists behind a variable that
defaults to off, because nothing terminates TLS yet). This removes the single most commonly
attacked surface on a public EC2 instance, and it costs almost nothing in convenience: Session
Manager also does port-forwarding, which is how Grafana and Sentinel are reached from a laptop.

**Storage is K3s's `local-path` provisioner on the root EBS volume.** PVCs become directories on
the node's disk. This survives pod recreation and instance reboot, and does not survive instance
replacement — a `terraform destroy`/`apply` cycle starts from an empty database. Stated plainly
rather than implied by "it's a demo": the honest description of this storage is "durable across
the failures Sentinel is meant to handle, not durable across the ones it is not".

**Kubernetes layout — a real Kustomize base with two overlays.**

```text
k8s/base/            shared manifests, configured for local development
k8s/overlays/local/  base + the committed demo Secrets (kind / minikube / Docker Desktop)
k8s/overlays/aws/    base + Traefik, ECR images pinned by SHA, real Secrets, Sentinel
```

The manifests moved out of `k8s/*` into `k8s/base/*` for a mechanical reason worth recording:
Kustomize refuses an overlay whose base is a parent directory containing that overlay, reporting
a cycle. There is no way to keep the manifests at the `k8s/` root and also have `k8s/overlays/aws`
build on them. **Local development is unchanged** — `k8s/kustomization.yaml` now points at
`overlays/local`, so `kubectl apply -k k8s/` and all three `scripts/deploy-*.sh` behave exactly as
they did in Phase 8.

The demo Secrets moved to `k8s/overlays/local/secrets/` rather than staying in the base, and this
is a security decision rather than tidiness. They contain literal committed passwords
(`sentinal`, `replace_me`), which is a fine trade on a laptop and is what makes local setup a
single command. Keeping them out of the base means the AWS overlay does not inherit them at all —
it cannot ship a known password to a public IP by forgetting an override, because the manifest is
not in its resource list. The AWS overlay generates its Secrets from gitignored `.env` files
instead, and `kubectl apply -k` fails with a missing-file error until they exist. Failing closed
beats defaulting to a published credential on an internet-facing host.

**CI/CD extended to actually deploy — `.github/workflows/ci-cd.yml`.**

Phase 11 built, tested and published images and honestly called out that it deployed nothing,
because there was nothing to deploy to. There is now:

- **Registry moved from GHCR to ECR**, four repositories under `sentinel-sre-demo/`
  (`citizen-service`, `notification-service`, `frontend`, `sentinel-ai`). Images are tagged with
  the git commit SHA. Nothing is deployed by `latest` — the running ReplicaSet has to be traceable
  back to a commit, because "which deploy caused this" is a question Sentinel asks and answers
  mechanically.
- **AWS authentication is GitHub OIDC federation** (`infra/terraform/github_oidc.tf`). There are
  no long-lived AWS access keys in repository secrets. The trust policy is scoped to this
  repository and branch, and the role's permissions are limited to the ECR repositories and the
  one instance it is allowed to send commands to.
- **Deployment goes through `aws ssm send-command`**, invoking `/usr/local/bin/sentinel-deploy.sh
  images <sha>` — a fixed script installed on the node at provisioning time. CI never connects to
  the Kubernetes API, never holds a kubeconfig, and cannot run an arbitrary command on the node:
  it can only ask SSM to run that one script with an image tag. The API server stays unreachable
  from the internet, which was the point of not exposing `:6443`.
- **`kubeconform` now validates both overlays**, not just the local one, so the AWS manifests get
  the same real-schema check the local ones have had since Phase 11.
- **A `test-sentinel-ai` job**, running Sentinel's own pytest suite alongside the two application
  suites.

**Chaos surface extended for the incident types Sentinel needs to distinguish.**

Two new fault fields on both services' chaos API: `cpu_burn` (bool) and `memory_leak_mb`
(int, 0–2048), exported as the gauges `chaos_cpu_burn` and `chaos_memory_leak_mb` alongside the
existing `chaos_*` metrics. The control API is otherwise unchanged, including the Phase 10
behaviour of returning 404 rather than 401 to an unauthenticated caller so the surface is not
advertised.

`scripts/incident-scenarios.sh` grew from five scenarios to nine: `db-outage`, `http-errors`,
`latency`, `notification-degradation`, `full-outage`, `high-cpu`, `memory-leak`, `crashloop`,
`bad-deployment`. The four new ones exist because they are the cases where Sentinel's *choice of
action* is the interesting part rather than its detection. `high-cpu` and `memory-leak` are
resource-exhaustion incidents where a restart is genuinely the right first move; `crashloop` is
one where it is not; `bad-deployment` is the one where the correct action is a rollback and
nothing else, which is the scenario the whole rollback path exists for. New Prometheus rules
(`HighCPUUsage`, `MemoryLeakSuspected`, `ChaosCPUBurn`, `ChaosMemoryLeak`,
`NotificationDispatchFailures`, plus `SentinelDown` and `SentinelEscalating` watching Sentinel
itself) come in through the AWS overlay's monitoring patch.

**Sentinel AI — `sentinel-ai/`, an autonomous SRE agent, in this repo.**

Through Phase 12 this project described Sentinel as "a separate platform built in its own
repository" and this repo as its sample workload. That split is gone: Sentinel is a FastAPI
service in `sentinel-ai/`, deployed by the AWS overlay into the same `citizen-portal` namespace as
the workload it watches, listening on `:8080`, receiving Alertmanager webhooks at
`/api/alerts/webhook`.

Its incident lifecycle is fixed and explicit:

```text
DETECTION -> INVESTIGATION -> CORRELATION -> ROOT CAUSE ANALYSIS
          -> REMEDIATION DECISION -> POLICY CHECK -> AUTONOMOUS EXECUTION
          -> RECOVERY VALIDATION -> DOCUMENTATION -> NOTIFICATION -> LEARNING
```

When recovery validation fails, it re-investigates, takes the next action off the ordered
candidate list, executes it and re-validates. When the list is exhausted, or when the action cap
for the incident is reached, it escalates. An agent that keeps inventing new things to try against
a service that is not recovering is strictly worse than one that stops and says so.

The layering is the security argument, and it is why an LLM being involved is defensible:

```text
LLM  ->  Decision Engine  ->  Policy Engine  ->  Remediation Engine  ->  Kubernetes API
```

The LLM analyses evidence and returns a *structured* recommendation. It never receives shell
access, never receives kubectl, and cannot name a target outside the allow-list — the action name
is parsed against a fixed enum and the target against a configured list, both in ordinary
deterministic code, before anything reaches the cluster. Everything downstream of the LLM is code
with no free-text path into it.

Four autonomous actions exist: `restart_deployment`, `rollback_deployment`, `scale_deployment`,
`reset_chaos_fault`. Confidence thresholds are 0.95 for rollback and 0.90 for the other three,
and rollback additionally requires that the deployment and namespace are allow-listed, that a
previous revision exists, that deployment history exists, that a recent deploy correlates with the
incident, that the rollback is reversible, and that recovery validation is available for the
target. If any precondition fails, the Decision Engine's next candidate is tried, or the incident
escalates.

**Rollback runs with no human approval step.** That is deliberate and is the point of the phase.
What bounds it is not an approval gate but three things that hold whatever the LLM says: the
allow-list (`citizen-service`, `notification-service`, `frontend` — and a frozen deny-list for
both Postgres deployments that no environment variable can override), the namespaced RBAC Role,
and the preconditions above.

RBAC is a namespaced `Role`, not `cluster-admin` and not a `ClusterRole`
(`k8s/overlays/aws/sentinel/namespace-rbac.yaml`). It reads pods, services, endpoints, configmaps,
events, deployments, replicasets and pod logs; it writes only `patch`/`update` on deployments and
`deployments/scale`, plus creating Events so its own actions show up in `kubectl get events`
alongside everything else. It cannot exec into a container, cannot delete anything (restarts are
done by patching the pod template, so no delete verb is needed at all), cannot read Secrets,
cannot touch PVCs or nodes, and cannot modify RBAC. It has no AWS permissions of any kind.
`citizen-postgres` and `notification-postgres` are not allow-listed, so a database incident
escalates to a human by design.

Recovery validation after every action checks deployment availability, pod readiness, HTTP health,
5xx error rate, p95 latency, CPU, memory, and the `chaos_*` gauges. The HTTP health check parses
the JSON `status` field rather than trusting the status code, because `/readyz` returns 200 with
`status: "degraded"` when a downstream dependency is broken — a validator that only looked at the
code would declare a still-broken service recovered. An incident is only marked RESOLVED after
validation passes.

GitHub integration creates issues, incident reports and postmortems, and can *propose* a fix. It
never modifies application code and never merges anything.

### What's missing / deferred on purpose

- **Nothing has been applied to AWS.** No `terraform apply`, and no `terraform plan` either — a
  plan needs real credentials against a real account, and there has been none. `terraform
  validate` and `terraform fmt` pass; that confirms the configuration is well-formed and
  internally consistent, not that AWS would accept it. Expect the first real plan to surface
  something: an AMI parameter path, a region without `t3a` capacity, an IAM policy that is
  narrower than the thing it needs to permit.
- **No pod has ever started on AWS.** The user-data bootstrap, the ECR credential refresh timer,
  the Traefik ingress patch and `scripts/deploy-aws.sh` are all first-run-untested, in exactly the
  sense Phases 7, 8, 9 and 12 each used the phrase before their own first real runs.
- **Sentinel has never run against a real cluster.** The lifecycle, the Policy Engine, the
  Decision Engine's ordering and the rollback preconditions are implemented and unit-tested with
  the Kubernetes and Prometheus clients stubbed. The autonomous remediation and rollback path is
  **not verified end-to-end.** Until a `bad-deployment` scenario has been run on a live cluster and
  watched all the way through to a validated rollback, every claim about Sentinel's behaviour in
  this file is a claim about code, not about observed behaviour.
- **No TLS and no DNS.** The portal is reachable over plain HTTP on an IP address. `:443` is
  wired behind a Terraform variable that defaults to off because nothing terminates TLS yet.
  Doing this properly needs a hostname first, then cert-manager or Traefik's own ACME resolver;
  putting a self-signed certificate on an IP address would be worse than plain HTTP, because it
  teaches whoever demos it to click through a browser warning.
- **No database backups.** Postgres runs in-cluster on a local-path PVC with no `pg_dump`
  schedule, no snapshot policy, and nothing that survives instance replacement. This is the
  sharpest edge of the "not production" caveat.
- **Terraform state is local.** No S3 backend, no DynamoDB lock table. One operator, one laptop,
  no concurrent applies — fine for that, and a genuine problem the moment a second person or a CI
  job needs to apply.
- **Sentinel runs as a single replica and is not safely horizontally scalable.** Incident
  deduplication is in-process and the incident store is node-local SQLite, so two replicas would
  each open their own incident for the same alert and neither would see the other's history. This
  is a design consequence, not an oversight — but it means Sentinel itself is a single point of
  failure in the system that is supposed to be watching for single points of failure.
- **Single node, no redundancy anywhere.** One instance, one AZ, one control plane. If the
  instance goes, everything goes, and Sentinel goes with it.
- **The LLM is optional and unset by default.** With no `OPENAI_API_KEY`, Sentinel runs fully
  rule-based: it still detects, investigates, correlates, decides, remediates and validates, and
  it records `llm_used=false` on the incident so a later reading of the record is honest about
  what produced the narrative. Nothing degrades silently.
- **No autoscaling, no HPA, no cluster autoscaler.** Replica counts are fixed and sized by hand
  to fit 2 vCPU; `MAX_REPLICAS=3` is what the node can actually absorb rather than a policy
  preference.
- **Image vulnerability scanning is still absent**, for the reasons in Phase 11's finding, which
  have not been revisited.

### Possible improvements

- Run `terraform plan`, then `apply`, then `scripts/deploy-aws.sh`, then
  `./scripts/incident-scenarios.sh bad-deployment`, and watch Sentinel roll it back. That single
  sequence is what converts this phase from "written" to "done", and until it happens nothing else
  on this list matters much.
- A hostname and TLS (Route 53 or any DNS provider, plus Traefik's ACME resolver), which also
  unblocks exposing Grafana with real authentication instead of a port-forward.
- Move Terraform state to S3 with DynamoDB locking before a second person touches it.
- Scheduled `pg_dump` to S3 with lifecycle expiry — the cheapest possible backup that is better
  than none.
- Make Sentinel horizontally scalable by moving the incident store and the dedup window out of
  the pod (Postgres, or leader election) — worth doing only once there is more than one node for
  a second replica to land on.
- A local Sentinel setup so it can be exercised on kind/minikube without AWS. Sentinel is only in
  the AWS overlay today, which means the fastest way to test it is also the slowest.
- Feed `scripts/incident-scenarios.sh` into CI as a scheduled synthetic-incident suite against
  the live node, asserting on Sentinel's incident records rather than only on alerts firing —
  Phase 12's "possible improvement" made concrete now that a long-lived cluster is meant to exist.

### Tech stack

Terraform (AWS provider 5.x, local state), AWS: VPC / Internet Gateway / public subnet / route
table / security group / EC2 / EBS / IAM / ECR / Systems Manager. K3s `v1.31.4+k3s1` with its
bundled Traefik, on Ubuntu 24.04. Kustomize base + overlays (`kubectl kustomize`, no standalone
binary needed). GitHub Actions with OIDC federation and `aws ssm send-command`. Sentinel:
FastAPI, `pydantic-settings`, `httpx`, the Kubernetes HTTP API, SQLite for incident history, and
optionally the OpenAI API. No new application dependencies in `citizen-service` or
`notification-service` beyond what the two new chaos fields needed.

---

## What's next

**Twelve of thirteen phases are implemented and Phase 13 is written but unapplied.** The gap is
specific and worth naming precisely, because it is the same gap Phases 8, 9 and 12 each carried
before someone finally ran them: everything in Phase 13 has been checked as thoroughly as it can
be without an AWS account attached — Terraform validates, both Kustomize overlays render and pass
`kubeconform`, Sentinel's lifecycle is unit-tested — and none of it has been executed. No
`terraform plan`, no instance, no pod, no incident.

The next step is therefore not another phase. It is:

1. `terraform plan` and `apply` in `infra/terraform/`, and fix whatever the first real plan
   surfaces.
2. `scripts/generate-aws-secrets.sh`, then `scripts/deploy-aws.sh <sha>` over SSM, and confirm the
   portal answers on the instance's public IP.
3. `./scripts/incident-scenarios.sh all`, and watch Phase 9's alerts fire on a real cluster for
   the first time.
4. `./scripts/incident-scenarios.sh bad-deployment`, and watch Sentinel correlate the deploy,
   roll it back with no human involved, validate the recovery, and write the incident up.

Only after (4) is any statement in this file about autonomous remediation a statement about
observed behaviour rather than about code that should work.

Two documents cover the details this file only summarises:

- **[`docs/aws-deployment.md`](docs/aws-deployment.md)** — the AWS architecture in full: what
  Terraform creates, why EKS/RDS/ALB/NAT Gateway were each declined, how the SSM-only
  administration path works, and the step-by-step deploy runbook.
- **[`docs/sentinel-integration.md`](docs/sentinel-integration.md)** — what Sentinel reads, what
  it is permitted to act on, how `request_id` ties logs to alerts, and how it tells a deliberately
  injected chaos fault from a real failure.

See also [`README.md`](README.md) for the project overview and a worked example incident, and
[`k8s/README.md`](k8s/README.md) for the manifest layout and both deployment paths.
