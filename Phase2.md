# Phase 2 — Notification Service

Adds a second microservice, **Notification Service**, and wires **Citizen Service** to call it
whenever a citizen-facing event happens (request submitted, request status changed).

> This is a demonstration project. It does not represent any real government system or process.

## Project status

- [x] Phase 1 — Citizen Service + PostgreSQL
- [x] **Phase 2 — Notification Service** (this phase)
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

Note: Phases 4 and 5 (Docker/Compose) are already substantially in place — both services have had
Dockerfiles and a shared `docker-compose.yml` since Phase 1/2 landed, so those phases mostly remain
as a checkpoint to revisit once the frontend (Phase 3) exists and the whole stack needs a final pass.

## Architecture (current)

```text
                    React Frontend  (Phase 3, not yet built)
                           |
                           | HTTP/REST
                           v
                Citizen Service  ------------>  Notification Service
                  FastAPI/Python    fire-and-       FastAPI/Python
                    |                forget              |
                    v                                    v
              PostgreSQL                          PostgreSQL
           (citizen_portal)                  (notification_service)
```

Each service owns its own database — no cross-service joins, no shared schema. Citizen Service
calls Notification Service over plain HTTP with an ID-only payload (`citizen_id`, `request_id`),
never a DB reference.

## Phase 2 — what's implemented

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

- `POST /api/requests` and `PUT /api/requests/{id}` now call Notification Service
  (`app/core/notifications.py: NotificationClient`) whenever a request is submitted or its status
  actually changes.
- The call is **fire-and-forget**: it runs as a FastAPI `BackgroundTask` *after* the response is
  already sent, wrapped in a broad `httpx.HTTPError` catch. A slow or completely unavailable
  Notification Service never adds latency to, or fails, the citizen-facing request. Failures are
  logged (`notification_dispatch_failed`) so this becomes an observable symptom for Sentinel later,
  rather than a citizen-service outage.
- `NotificationClient` is injected via a FastAPI dependency (`get_notification_client`), the same
  pattern already used for `get_db`, so tests can swap in a no-op fake instead of making real network
  calls — see `tests/conftest.py: _FakeNotificationClient`. Two new tests
  (`test_create_request_dispatches_notification`, `test_update_request_status_dispatches_notification`)
  assert the dispatch happens with the right `event_type`.

## Tech stack (Phase 2 additions)

Same stack as Phase 1 (Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0, Alembic, PostgreSQL 16,
Prometheus instrumentation, structured JSON logging, pytest against in-memory SQLite) — no new
technologies introduced, only a new service built to the same conventions.

## Running locally

```bash
cp .env.example .env   # if starting fresh; the existing .env already has NOTIFICATION_* vars
docker compose up --build
```

Once it's up:

```text
Citizen Service:       http://localhost:8000
Notification Service:  http://localhost:8001
Notification Swagger:  http://localhost:8001/docs
Notification health:   http://localhost:8001/healthz
Notification metrics:  http://localhost:8001/metrics
Notification Postgres: localhost:5433
```

`notification-service` runs its own `alembic upgrade head` on boot, independent of `citizen-service`'s
migration, against its own Postgres instance (`notification-postgres`, port 5433 on the host so it
doesn't collide with `citizen-service`'s Postgres on 5432).

`citizen-service` waits for its own Postgres to be healthy but only waits for `notification-service`
to have *started* (not be healthy) before booting — consistent with the fire-and-forget design: the
citizen portal should come up even if notifications are degraded.

### Try it out

```bash
# Register + login (Phase 1)
curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"full_name":"Amina Ali","national_id":"29901010112233","email":"amina@example.com","phone":"+201000000000","password":"SuperSecret123"}'

# Submit a request — this triggers a notification in the background
curl -X POST http://localhost:8000/api/requests \
  -H "Authorization: Bearer TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"service_id":"SERVICE_ID"}'

# Check that the notification landed
curl "http://localhost:8001/api/notifications?citizen_id=CITIZEN_ID"
```

## Running the test suites

Both services run independently against in-memory SQLite — no Docker/Postgres required for either:

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

## Project structure (Phase 2 additions)

```text
digital-citizen-portal/
├── docker-compose.yml         # now also runs notification-postgres + notification-service
├── .env                       # now also has NOTIFICATION_DATABASE_*, CHAOS_MODE, CHAOS_FAILURE_RATE
├── Phase1.md
├── Phase2.md                  # this file
├── README.md
├── citizen-service/
│   └── app/
│       ├── core/notifications.py   # NEW — NotificationClient + get_notification_client dependency
│       └── routers/requests.py     # UPDATED — dispatches notifications on submit/status-change
└── notification-service/            # NEW — entire service
    ├── Dockerfile
    ├── requirements.txt
    ├── alembic.ini
    ├── alembic/
    │   ├── env.py
    │   ├── script.py.mako
    │   └── versions/0001_initial_schema.py
    ├── app/
    │   ├── main.py
    │   ├── core/
    │   │   ├── config.py         # own DATABASE_*, CHAOS_MODE, CHAOS_FAILURE_RATE settings
    │   │   ├── database.py
    │   │   ├── logging_config.py
    │   │   └── sender.py          # simulated delivery + chaos-mode failure injection
    │   ├── models/notification.py
    │   ├── schemas/notification.py
    │   └── routers/
    │       ├── health.py
    │       └── notifications.py
    └── tests/                      # pytest suite (SQLite-backed)
```

## Design notes / assumptions

- **One database per microservice.** `notification-service` gets its own PostgreSQL instance
  (`notification-postgres`, its own volume, its own port on the host) rather than a second schema
  bolted onto Citizen Service's database. This is the realistic pattern and keeps the two services
  independently deployable/scalable — a deliberate design choice for a platform meant to simulate
  production-like failure domains for Sentinel to reason about.
- **No foreign keys across services.** `Notification.citizen_id` / `Notification.request_id` are
  plain UUID columns with no `ForeignKey` — they reference rows that live in a different database
  entirely. Referential integrity across service boundaries is the producer's responsibility
  (citizen-service only ever sends IDs it knows are valid), not something the database can enforce.
- **Fire-and-forget over synchronous call or message queue.** A message broker (e.g. RabbitMQ/Kafka)
  would be the "more correct" production pattern for guaranteed delivery, but it's out of scope for
  this phase — the goal here is a realistic *failure surface* (a downstream HTTP dependency that can
  degrade or go down) for Sentinel to detect and reason about, not a fully durable notification
  pipeline. This tradeoff is worth revisiting if a later phase wants to demonstrate queue-based
  resilience patterns specifically.
- **Simulated delivery, not a real provider.** No SendGrid/Twilio/etc. integration — keeps the whole
  stack runnable offline and deterministic for tests, while the chaos-mode hook still gives Sentinel
  something realistic to detect later.
- **CORS on notification-service** is included (locked to `http://localhost:3000`, same as
  citizen-service) even though nothing calls it from a browser yet, in case a future admin view
  queries it directly.

## Troubleshooting

- **`notification-service` container keeps restarting** — same failure mode as Phase 1's Postgres
  troubleshooting note: if `NOTIFICATION_DATABASE_PASSWORD` changed after
  `notification_postgres_data` volume was created, run `docker compose down -v` and rebuild.
- **Notifications never show up but requests succeed fine** — this is by design if
  `notification-service` is down or slow; check `docker compose logs citizen-service` for
  `notification_dispatch_failed` log lines, and `docker compose logs notification-service` /
  `docker compose ps` to confirm the service itself is healthy.

## Future improvements

See the Project status checklist above — Frontend, Kubernetes manifests, the observability stack,
chaos engineering endpoints (beyond the notification-delivery hook already in place), and CI/CD are
all planned in subsequent phases.
