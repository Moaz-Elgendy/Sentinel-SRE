# Digital Citizen Services Portal

A realistic, intentionally manageable cloud-native application that simulates a government
digital services portal. It's built to be the **sample production workload monitored by
Sentinel AI** (an autonomous SRE platform, built separately) — so it's designed from day one
for observability, health checks, and controlled failure injection.

> This is a demonstration project. It does not represent any real government system or process,
> and national IDs / documents used here are fake/generated.

## Project status

This repo is being built incrementally, phase by phase, per the build plan. Each phase is fully
working and tested before the next one starts.

- [x] **Phase 1 — Citizen Service + PostgreSQL** (this phase)
- [ ] Phase 2 — Notification Service
- [ ] Phase 3 — React Frontend
- [ ] Phase 4 — Dockerize everything
- [ ] Phase 5 — Docker Compose (full stack)
- [ ] Phase 6 — Health checks, metrics, structured logs, request IDs
- [ ] Phase 7 — Kubernetes manifests
- [ ] Phase 8 — Deploy to Kubernetes
- [ ] Phase 9 — Prometheus / Grafana / Loki / Alertmanager
- [ ] Phase 10 — Chaos / failure injection
- [ ] Phase 11 — CI/CD
- [ ] Phase 12 — End-to-end incident simulations

## Architecture (target, full system)

```text
                    React Frontend
                           |
                           | HTTP/REST
                           v
                Citizen Service
                  FastAPI/Python
                    /       \
                   /         \
                  v           v
           PostgreSQL    Notification Service
                            FastAPI/Python
```

Right now, only **Citizen Service** and **PostgreSQL** exist and run.

## Phase 1 — what's implemented

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
  tables — `documents` table exists now for Phase 1's forward-compatible schema, upload endpoint itself
  lands in a later phase)
- **20 passing pytest tests** covering auth, profile, services, requests, and health endpoints

Not yet built: Notification Service, frontend, document upload endpoint, Kubernetes manifests,
observability stack, chaos endpoints, CI/CD. These come in later phases.

## Tech stack (Phase 1)

- Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0, Alembic
- PostgreSQL 16
- JWT auth (`python-jose`) + bcrypt password hashing (`passlib`)
- Prometheus instrumentation
- Docker + Docker Compose
- pytest (tests run against an in-memory SQLite DB — no live Postgres required to run the suite)

## Running locally

```bash
git clone <repository>
cd digital-citizen-portal

cp .env.example .env
# edit .env and set a real DATABASE_PASSWORD and JWT_SECRET for anything beyond local testing

docker compose up --build
```

Once it's up:

```text
Citizen Service:  http://localhost:8000
Swagger UI:       http://localhost:8000/docs
Health check:     http://localhost:8000/healthz
Readiness check:  http://localhost:8000/readyz
Metrics:          http://localhost:8000/metrics
PostgreSQL:       localhost:5432
```

On startup, the `citizen-service` container automatically runs `alembic upgrade head` (creates/updates
the schema) and seeds the six predefined government services before starting the API.

### Try it out

```bash
# Register
curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"full_name":"Amina Ali","national_id":"29901010112233","email":"amina@example.com","phone":"+201000000000","password":"SuperSecret123"}'

# Login
curl -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"amina@example.com","password":"SuperSecret123"}'
# -> {"access_token": "...", "token_type": "bearer"}

# Browse services (no auth needed)
curl http://localhost:8000/api/services

# Submit a request (replace TOKEN and SERVICE_ID)
curl -X POST http://localhost:8000/api/requests \
  -H "Authorization: Bearer TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"service_id":"SERVICE_ID"}'
```

## Running the test suite

The test suite runs against an isolated in-memory SQLite database, so no Docker/Postgres is required:

```bash
cd citizen-service
pip install -r requirements.txt
DATABASE_PASSWORD=test JWT_SECRET=test python -m pytest -v
```

Expected: **20 passed**.

## Project structure (Phase 1)

```text
digital-citizen-portal/
├── docker-compose.yml
├── .env.example
├── README.md
└── citizen-service/
    ├── Dockerfile
    ├── requirements.txt
    ├── alembic.ini
    ├── alembic/
    │   ├── env.py
    │   ├── script.py.mako
    │   └── versions/0001_initial_schema.py
    ├── app/
    │   ├── main.py              # FastAPI app, middleware, router wiring
    │   ├── seed.py               # Seeds the government services catalog
    │   ├── core/
    │   │   ├── config.py         # Settings from environment variables
    │   │   ├── database.py       # SQLAlchemy engine/session
    │   │   ├── security.py       # Password hashing + JWT
    │   │   ├── deps.py           # get_current_citizen dependency
    │   │   └── logging_config.py # Structured JSON logging
    │   ├── models/                # SQLAlchemy models
    │   ├── schemas/                # Pydantic request/response models
    │   └── routers/                # auth, profile, services, requests, health
    └── tests/                      # pytest suite (SQLite-backed)
```

## Design notes / assumptions

- **`documents` table exists but no upload endpoint yet.** The schema is in place (per the spec's
  forward-compatibility goal) but the `POST` upload route is deferred — it doesn't belong to the
  "Citizen Service + PostgreSQL" scope of Phase 1 and will land with local/volume-based storage in
  a later phase, structured so S3 can be swapped in afterward without a schema change.
- **`required_documents` is stored as JSON, not a native Postgres `ARRAY`.** This keeps the schema
  portable so the test suite can run against SQLite without a live Postgres instance, while still
  working correctly against Postgres in Docker/Kubernetes.
- **Notification Service is not called yet.** Request submission currently logs the event instead
  of making an HTTP call to `NOTIFICATION_SERVICE_URL` — that integration is Phase 2's job, once the
  Notification Service actually exists to receive it.
- **CORS** is currently locked to `http://localhost:3000` (where the Phase 3 React frontend will run).

## Troubleshooting

- **`citizen-service` container keeps restarting / can't connect to DB** — Postgres has a healthcheck
  and `citizen-service` waits for it (`depends_on: condition: service_healthy`), but if you changed
  `DATABASE_PASSWORD` in `.env` after the Postgres volume was already created, Postgres won't pick up
  the new password. Run `docker compose down -v` to reset the volume, then `docker compose up --build`.
- **`alembic upgrade head` fails on first boot** — usually means Postgres wasn't ready yet despite the
  healthcheck; check `docker compose logs postgres`.

## Future improvements

See the Project status checklist above — Notification Service, frontend, Kubernetes manifests, the
observability stack, chaos engineering endpoints, and CI/CD are all planned in subsequent phases.
