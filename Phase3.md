# Phase 3 — React Frontend

Adds the citizen-facing web UI: a React SPA that talks to Citizen Service over its existing
`/api/*` endpoints. No backend changes were needed for this phase — Citizen Service's CORS config
already allowed `http://localhost:3000` since Phase 1.

> This is a demonstration project. It does not represent any real government system or process.

## Project status

- [x] Phase 1 — Citizen Service + PostgreSQL
- [x] Phase 2 — Notification Service
- [x] **Phase 3 — React Frontend** (this phase)
- [ ] Phase 4 — Dockerize everything
- [ ] Phase 5 — Docker Compose (full stack)
- [ ] Phase 6 — Health checks, metrics, structured logs, request IDs
- [ ] Phase 7 — Kubernetes manifests
- [ ] Phase 8 — Deploy to Kubernetes
- [ ] Phase 9 — Prometheus / Grafana / Loki / Alertmanager
- [ ] Phase 10 — Chaos / failure injection
- [ ] Phase 11 — CI/CD
- [ ] Phase 12 — End-to-end incident simulations

Note (carried over from Phase 2): both backend services already have Dockerfiles and are wired into
the root `docker-compose.yml`. Phase 4/5 remain as the checkpoint to add the frontend to that same
Compose file with an nginx-served production build, and do a final pass across all three services.

## Architecture (current)

```text
                    React Frontend  (Vite + React Router, this phase)
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

The frontend never talks to Notification Service directly — it only reflects state that Citizen
Service already exposes (a request's current status). Notification Service stays a purely
server-to-server dependency, consistent with Phase 2's design.

## Phase 3 — what's implemented

**`frontend/`** — a Vite + React 19 single-page app:

- **Auth** — Register and Login pages backed by `POST /api/auth/register` / `POST /api/auth/login`.
  JWT is kept in `localStorage` and attached to every request via an axios interceptor. A 401
  response from any endpoint automatically clears the local session (`app/api/client.js`).
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
  (`components/ProtectedRoute.jsx`).
- Loading states, inline error banners (FastAPI's `{"detail": ...}` / Pydantic validation array
  shapes are both normalized into readable text — `extractErrorMessage`), and empty states
  throughout, rather than blank screens or unhandled promise rejections.

## Tech stack (Phase 3)

- React 19, React Router 6, Axios
- Vite (dev server + production build)
- Plain CSS (no framework) — `src/index.css` — kept deliberately small and hand-written rather than
  pulling in Tailwind/MUI for a project this size
- No TypeScript in this pass — the API surface is small enough that plain JS with the API modules in
  `src/api/` (one file per resource) keeps the request/response shapes obvious without the build
  overhead; worth revisiting if the app grows.

## Running locally

Frontend against the full Docker Compose stack:

```bash
docker compose up --build          # brings up both backend services + their databases
cd frontend
npm install
npm run dev                        # http://localhost:3000
```

The dev server runs on port 3000 specifically because Citizen Service's CORS allow-list
(`app/main.py`) is already locked to `http://localhost:3000` — no proxy config needed.

To point the frontend at a non-default backend location, copy `frontend/.env.example` to
`frontend/.env` and set `VITE_API_BASE_URL`.

### Try it out

1. Open `http://localhost:3000`
2. Register a new citizen account
3. Browse **Services**, submit a request for one
4. Open **My Requests** to see it, click into it for the detail view
5. Check `http://localhost:8001/api/notifications` (or `docker compose logs notification-service`)
   to see the `request_submitted` notification Citizen Service dispatched in the background

## Running the frontend build/lint

```bash
cd frontend
npm install
npm run lint     # oxlint
npm run build     # production build to frontend/dist/
```

Both were run against this code before it shipped: lint reports zero errors (one expected
react-refresh advisory warning on the auth context module, not a bug), and the production build
completes cleanly (~95 KB gzipped JS).

## Project structure (Phase 3 additions)

```text
digital-citizen-portal/
├── Phase3.md                  # this file
└── frontend/                   # NEW
    ├── index.html
    ├── vite.config.js          # dev server pinned to port 3000 (see CORS note above)
    ├── .env.example
    └── src/
        ├── main.jsx             # entry, wraps App in BrowserRouter
        ├── App.jsx              # route table
        ├── index.css            # all app styling
        ├── api/
        │   ├── client.js        # axios instance, JWT injection, 401 handling, error extraction
        │   ├── auth.js
        │   ├── profile.js
        │   ├── services.js
        │   └── requests.js
        ├── context/
        │   └── AuthContext.jsx  # session state, login/register/logout
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

## Design notes / assumptions

- **Citizens can't set their own request status.** The backend's `PUT /api/requests/{id}` accepts
  `status` and `employee_note`, but that's a caseworker/admin action with no role separation built
  yet (Phase 1 has no employee auth). The frontend's request detail page is deliberately read-only —
  it would be misleading to expose a status dropdown a citizen shouldn't actually be able to use
  meaningfully. An employee-facing UI (with its own auth) is a natural candidate for a later phase.
- **Registration logs the citizen in immediately.** `POST /api/auth/register` doesn't return a
  token, so `AuthContext.register()` chains a login call right after, rather than sending someone
  back to a login form to re-type the password they just chose.
- **No client-side caching/state library.** Each page fetches what it needs in a `useEffect` and
  keeps local component state. React Query or similar would pay for itself if more pages start
  sharing the same data (e.g. the services list, fetched separately by both `ServicesPage` and
  `RequestsPage` today) — worth reconsidering if Phase 3's scope grows.
- **JWT in `localStorage`, not an httpOnly cookie.** Simplest option for a demo SPA talking to a
  separate-origin API with no server-side session; an httpOnly-cookie + CSRF-token setup would be
  the hardened choice for a real deployment.

## Troubleshooting

- **Frontend loads but every request fails / CORS error in the console** — confirm
  `citizen-service` is actually running on `localhost:8000` and that the frontend dev server is on
  `localhost:3000` (not some other port Vite picked because 3000 was taken) — CORS is locked to
  that exact origin.
- **"My Requests" shows nothing after submitting** — submitting a request and dispatching its
  notification are decoupled (see Phase 2); an empty requests list specifically means the
  `POST /api/requests` call itself didn't succeed — check the browser console/network tab, not
  `notification-service`'s logs.

## Future improvements

See the Project status checklist above — Kubernetes manifests, the observability stack, chaos
engineering, and CI/CD are all planned in subsequent phases. Within the frontend specifically: an
employee/admin view for managing request status, document upload once that backend endpoint exists,
and a "recent notifications" panel once there's a sensible way to expose Notification Service data
to the browser without calling it directly.
