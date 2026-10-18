# Sentinel GUI v1.3 — Refinement, Reliability & Operational UX

Self-directed refinement pass over `sentinel-gui/`, per `sentinel_gui_v1_3_refinement_prompt.md`.
Scope was GUI-only throughout: nothing in `sentinel-ai/` (the backend another
developer owns) was modified. Where the GUI exposes a genuine backend
inconsistency, it is documented below rather than papered over with a
frontend-only workaround.

Before touching any code, the whole GUI was audited end to end — routing,
pages, API client, hooks, state, polling/SSE, the incident data model,
feedback, logs, config pages, and error/loading/empty states. The headline
finding of that audit: this codebase already implements most of what the
v1.3 brief asks for. The Command Center's "act → numbers → what's
happening → what Sentinel's done" ordering, the Sentinel-vs-environment
health split, the Observe→Diagnose→Decide case-step hierarchy, the
expandable Action Ledger, and the config pages' shared loading/error/review
shell are all already in place and already good. This pass therefore
fixed two real, user-visible defects (log ordering, escalation stickiness),
added the two features the brief named explicitly as missing (a "Sentinel
Decision" summary card, a compact Evidence summary), made one small
accessibility consistency fix, and — per the brief's instruction to
document rather than invent — surfaced a genuine backend data-consistency
bug and a handful of orphaned frontend files, without touching either.

## Fixed

- **Log ordering (newest-first), `sentinel-gui/src/hooks/useLogsStream.js`,
  `sentinel-gui/src/components/logs/LogViewer.jsx`.** `GET /api/logs` already
  returns most-recent-first (verified against `app/core/log_capture.py`'s
  actual implementation, not just its docstring), but the frontend reversed
  that order on load and appended new live lines at the bottom. Rewrote
  `useLogsStream` to keep the API's order as-is on load, prepend live
  single-line arrivals, and correctly reverse the pause-buffer before
  prepending it on resume (buffered lines arrive oldest→newest during a
  pause; they need reversing to land newest-first). Flipped `LogViewer`'s
  "follow" mechanic to pin at the top (`scrollTop = 0`) instead of the
  bottom, and moved/relabelled the "jump to latest" affordance to the
  top-right with an up-arrow.

- **Escalation stickiness ("previously escalated but resolved" showing as
  still escalated), `sentinel-gui/src/utils/status.js`,
  `sentinel-gui/src/utils/incident.js`,
  `sentinel-gui/src/components/incident/LiveFlowDiagram.jsx`,
  `sentinel-gui/src/components/incident/TimelineTab.jsx`,
  `sentinel-gui/src/styles/pages.css`.** Root-caused to a genuine backend
  inconsistency (see Escalation Audit below) that leaves `escalated: true`
  set on an incident whose `status` has already moved to a resolved value.
  The frontend was checking the raw `escalated` boolean *before* checking
  whether `status` was already resolved — the exact inverse of the
  precedence the brief's Case A calls for. Fixed the precedence everywhere
  it mattered: `effectiveIncidentStatus`, `isAwaitingHuman`, `durationLabel`,
  `lifecycleOutcome`, `incidentOutcome`, and the lifecycle rail's
  `policyBlocked`/`validationFailed` flags now all treat a resolved/
  auto_resolved `status` as authoritative over a possibly-stale `escalated`
  flag. `LiveFlowDiagram` and `TimelineTab` now render a past escalation on
  an otherwise-resolved incident as calm historical context ("Previously
  escalated…") rather than a live alarm — the exact "RESOLVED / Previously
  escalated" distinction the brief asked for.

- **"Sentinel decision" summary card (new),
  `sentinel-gui/src/components/incident/DecisionSummaryCard.jsx`,** wired
  into `IncidentDetailPage.jsx` right under the Lifecycle panel. A one-glance
  synthesis of the diagnosis and the chosen action — the two things a reader
  otherwise has to open two separate case-step panels to get — built purely
  from data those panels already read (`incident.hypothesis`,
  `actionSummary()`), so it never states a fact the detailed panels don't
  already show. Deliberately leaves the outcome (recovered/awaiting/in
  progress) to the existing `OutcomeStrip` just above it, so the two don't
  repaint the same state twice.

- **Compact Evidence summary, `sentinel-gui/src/components/incident/EvidenceTab.jsx`.**
  Added an at-a-glance stat strip (replicas available, pods ready, pod
  restarts, Kubernetes event count, revision count, log sample count) above
  the existing raw pods/revisions/events/health tables, so a reader gets the
  gist without parsing every row. Every number is one already computed
  from data the tables below it already render — no new fields, no new API
  calls.

- **Keyboard activation consistency,
  `sentinel-gui/src/pages/ActionHistoryPage.jsx`,
  `sentinel-gui/src/pages/IncidentsListPage.jsx`.** Their clickable table
  rows handled Enter but not Space; `LogViewer`'s own expandable rows already
  handled both. Added Space (with `preventDefault` so it doesn't also
  scroll the page) to both, matching the pattern that was already correct
  elsewhere.

### Found, documented, deliberately not changed

- **Two design systems.** `SentinelLivePage.jsx`, `AuthorizationPanel.jsx`,
  `ConfigParts.jsx` and a few others still use an older, mixed-case,
  plain-CSS component set (`components/ui/Tag.jsx`, `Button.jsx`, `Field.jsx`,
  `AlertBanner.jsx`) rather than the shadcn/ui set the rest of the app uses.
  Both are live, both work; migrating one to the other would be a redesign,
  which the brief explicitly rules out. Left as-is.
- **Five orphaned components, zero importers anywhere in `src/`:**
  `components/incident/LiveFlowDiagram.jsx` (fixed above despite this — the
  fix is harmless either way, but it is not currently rendered by any page),
  `components/incident/EvidencePanel.jsx`, `components/incident/ReasoningPanel.jsx`,
  `components/incident/DecisionActionPanel.jsx`, and the standalone
  `components/incident/FeedbackForm.jsx` (superseded by `FeedbackTab.jsx`'s
  own inline `FeedbackForm`). These read like remnants of an earlier design
  generation, pre-dating the current case-step system. Not deleted — removal
  wasn't asked for, and this environment's permission model blocks file
  deletion outright without an explicit approval step this session couldn't
  obtain. Worth a deliberate cleanup pass.

## Feedback Audit

- **Endpoints:** `POST /api/incidents/{incident_id}/feedback/diagnosis`,
  `POST /api/incidents/{incident_id}/feedback/remediation`,
  `GET /api/incidents/{incident_id}/feedback`
  (`sentinel-ai/app/routers/feedback.py`) — admin-JWT gated.
- **Request shape:** diagnosis — `{correct: bool, actual_root_cause?: RootCause, note?: str}`;
  remediation — `{useful: bool, suggested_action?: RemediationAction, note?: str}`.
  `actual_root_cause`/`suggested_action` are typed as the real backend enums,
  so a value Sentinel could never itself produce is rejected by FastAPI
  validation before it reaches the store.
- **Persistence: YES.** Read `sqlite_store.py`'s `create_feedback()` in full —
  a genuine `INSERT INTO incident_feedback (...) VALUES (...)` followed by
  `conn.commit()`, no upsert, no uniqueness constraint (only non-unique
  indexes on `incident_id` and `kind`), so it's an honest append-only history,
  not a stub.
- **Refresh-persistence test: PASS** — verified by full source trace, not a
  live browser click (no reachable running Sentinel deployment from this
  session). `FeedbackTab.jsx`'s submit handler is `await
  submitDiagnosisFeedback(...).then(refresh)`, and `refresh()` re-fetches via
  the real `GET .../feedback` endpoint; the form's "Thanks, feedback
  recorded" confirmation only appears after that re-fetch resolves, not
  merely after the POST responds — so the UI's success state is already
  gated on proof of persistence, which is stronger than the brief asked for.
- **Frontend behavior:** real POST → real GET re-fetch → history list
  re-renders with the new row. On failure: inline error message, the user's
  in-progress answer is preserved (not reset), no optimistic success. The
  page copy and the backend docstring both explicitly say feedback never
  changes Sentinel's behavior — "learning" is never claimed anywhere in the
  GUI.

## Escalation Audit

- **Backend fields** (`sentinel-ai/app/models/incident.py`): `status` (enum,
  including `RESOLVED`, `ESCALATED`, `AUTO_RESOLVED`), `escalated: bool`,
  `escalation_reason`, `escalation_record` (the most recent escalation's
  detail — never auto-cleared to empty), `escalation_history` (appended to
  only when `orchestrator.py`'s `reconsider()` reopens an incident).
- **Backend dependency (found, not fixed — out of this pass's scope):**
  `IncidentManager.handle_resolved()` / `_CLOSED_STATUSES` in
  `app/lifecycle/incident_manager.py` excludes `ESCALATED` from the tuple of
  statuses it treats as "already closed." So when an Alertmanager "resolved"
  webhook arrives for an incident currently sitting at `status=ESCALATED`,
  it sets `status=AUTO_RESOLVED` **without clearing `escalated` or
  `escalation_reason`.** That's the real, concrete source of the "previously
  escalated but resolved is currently broken" defect the brief called out —
  a genuine backend data-consistency gap, not a frontend one. Recommend the
  backend owner clear (or leave status at `ESCALATED` and rely on) those
  fields consistently whenever a status transition moves an incident away
  from `ESCALATED`.
- **Frontend precedence (fixed this pass):** a resolved/auto_resolved
  `status` now wins outright over a possibly-stale `escalated` boolean,
  everywhere that boolean was being read (see Fixed, above).
- **Previously-escalated-then-resolved: PASS** — by source trace. With the
  fix, such an incident renders as Resolved/Cleared everywhere (status
  badge, lifecycle rail, attention panel, duration label, outcome strip),
  with a separate, visually muted "Previously escalated at …" marker sourced
  from `escalation_record`, never as a live "Needs you" alarm. Not exercised
  against a live running incident in this environment — no reachable
  Kubernetes/Prometheus/Loki/Alertmanager/FastAPI stack to generate one.

## Logs Audit

- **API ordering:** `GET /api/logs` (`sentinel-ai/app/routers/logs.py`) is
  most-recent-first — confirmed against `core/log_capture.py`'s actual
  `query_logs()` implementation (reads the live rotating file backward, then
  older backups), not only its docstring.
- **Frontend ordering (before):** reversed the already-correct order on
  initial load, and appended (rather than prepended) live SSE lines —
  net effect, oldest-first on load and newest silently added at the bottom.
- **Frontend ordering (after):** no reversal on load; live lines prepend;
  the pause-buffer is reversed before prepending on resume, since it
  accumulates oldest→newest during a pause.
- **Newest-first: PASS** (source trace).
- **Live insertion: PASS** (source trace) — new lines prepend without
  forcing a scroll, and the "follow" pin only engages when the reader is
  already within 48px of the top, so a reader scrolled down into history
  keeps their place.
- Not exercised in a live browser session — verified by tracing the exact
  data flow from `log_capture.py`'s file reads through the SSE payload to
  `useLogsStream`'s state updates and `LogViewer`'s render.

## Tests

Environment note first, since it affects how to read everything below: this
device's mounted repo folder is a FUSE mount, and running `vite build`'s
native esbuild binary directly on it crashed with a `Bus error` (a known
class of failure for native binaries mmapping themselves over FUSE — not a
code defect). `npm ci` also failed there with `EPERM` on an unrelated
`rmdir` of an unused optional platform binary, for the same
mounted-filesystem reason. Worked around by staging the source (excluding
`node_modules`/`dist`) into this session's own cloud sandbox — an ordinary
filesystem — and running the full toolchain there instead:

- `npm ci` — succeeded: 198 packages, 0 vulnerabilities.
- `npm run build` (`vite build`) — **succeeded**: 2,754 modules transformed,
  every chunk emitted, no errors, in 940ms. This is the real compile check
  on every file this pass touched or added.
- `npm run lint` (`oxlint`) — **passed**, exit 0. Two pre-existing warnings
  remain (`react(set-state-in-effect)` in `context/AuthContext.jsx` and
  `hooks/useLiveIncident.js`), both in files untouched by this pass; no new
  warnings were introduced by any change made here.
- **No unit, component, or E2E test suite exists** for `sentinel-gui` (no
  `*.test.*`/`*.spec.*` files, no Cypress/Playwright config). None were
  skipped — there is nothing to run. Said plainly rather than silently.
- **Docker image build:** not attempted as a real `docker build` — no Docker
  daemon is reachable from this session (`docker info` reports no daemon
  socket). The multi-stage `Dockerfile`'s build stage runs exactly
  `npm ci && npm run build`, both independently verified above; the serve
  stage is a stock `nginx:1.27-alpine` copying `dist/`. Not claiming a Docker
  build succeeded when it wasn't run.
- **Interaction-level checks** (log ordering + live arrival, the feedback
  round-trip including a reload, the ACTIVE→ESCALATED→RESOLVED transition)
  were verified by full source-level tracing of the real data flow — backend
  field shapes → API response shapes → frontend state transitions → rendered
  output — not by clicking through a live running instance. There is no
  reachable Sentinel deployment (cluster, Prometheus, Loki, Alertmanager,
  the FastAPI backend) from this session to click through. Stated plainly
  rather than implied as a live test.

### Housekeeping note

A build-validation step needed to copy the GUI's source out of this device
to a normal filesystem to work around the FUSE/native-binary issue above.
The temporary archive was written to `sentinel-gui/dist/.audit-tmp/` (inside
the already-`.gitignore`d `dist/` directory, so it was never at risk of
being committed) and has been zeroed out; this environment's permission
model didn't allow actually deleting it. It's safe to delete
`sentinel-gui/dist/.audit-tmp/` by hand.
