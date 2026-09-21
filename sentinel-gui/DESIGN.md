# Sentinel Control Center: design

This documents the redesign of `sentinel-gui`: what it is for, how it is organised, the design system, and how every capability of the previous GUI maps onto the new one. It replaces `DESIGN_SYSTEM.md`.

No Figma file accompanies it. The design was worked out in this document and in code, because no Figma workspace or API access was available while building it.

## The question the UI answers

> What does an SRE need to understand in the first few seconds?

Every screen is ordered by urgency: **do I need to act → what is happening → what did Sentinel do and why → the detail behind it.**

The lifecycle Sentinel runs is the spine of the product, and the UI draws it the same way everywhere:

`Detect → Evidence → Correlate → Diagnose → Decide → Policy → Act → Validate → Document → Outcome`

An incident page tells the story in the order the AI reasons: **Evidence → Diagnosis → Decision & action → Result**. Nothing is a black box: confidence, the policy verdict, each policy check, what was refused and why, and whether recovery was confirmed are all on screen.

## Information architecture

| Area | Route | Replaces |
|---|---|---|
| **Command center** | `/` | Dashboard + Sentinel Live |
| **Incidents** | `/incidents` (`?view=all\|active\|attention\|resolved`) | Incidents list |
| **Incident workspace** | `/incidents/:id` (`?tab=evidence\|timeline\|logs\|feedback`) | Incident detail |
| **Action ledger** | `/actions` | Action history |
| **Sentinel logs** | `/logs` | Sentinel logs |
| **Environment** | `/environment` | Environment |
| **Performance** | `/performance` | Performance |
| **Guardrails and configuration** | `/policies`, `/rca-config`, `/remediation-config`, `/ai-config`, `/monitoring-config`, `/config-history` | The six Administration pages, now one area with tabs |
| **Chaos scenarios** | `/demo` | Demo utilities (unchanged in purpose) |

`/live` redirects to `/`. All previous config URLs still work.

## Capability parity

Nothing was removed. Where a page merged, its function moved:

- **Sentinel Live → command center:** state headline is the posture banner; watchers are the "Data sources" row; the live feed is "Live activity"; concurrent active incidents and the LLM reasoner's health, which the old page never showed, are surfaced.
- **Authorize one action → incident "needs you" panel:** same endpoint, same single-use scoped grant, same replica option for scale, same history.
- **Config review flow:** still preview → diff → optional reason → confirm, now in a dialog. The dry-run switch still starts the same review.
- **Feedback, reports, restore-from-history, connectivity test, chaos runner:** all present.

Additions, all backed by data the API already returned or an endpoint that already existed:

- Re-investigate button (`POST /api/incidents/{id}/reinvestigate`).
- Rejected actions and required confidence on escalations (`escalation_record`).
- Pods, Kubernetes events and revision history (`evidence`).
- Groq provider fields on the AI page.
- Recurrence links, notification results, validation checks.
- Expandable structured log fields.
- ⌘K command palette.
- Light and dark themes.

## Design principles

1. **Colour means state.** Surfaces, text and primary buttons are neutral. Hue appears only for ok, warn, bad and in-progress, so anything coloured is telling you something.
2. **Never colour alone.** Every status pairs a hue with an icon and a word (`StatusBadge`, `SeverityBadge`, the rail).
3. **Motion means live.** The animated lifecycle segment and pulsing dots exist only while Sentinel is genuinely working. `prefers-reduced-motion` is honoured.
4. **Honest about what isn't known.** Health "unknown" is not drawn as green. The banner says "Checking…" until data arrives. Metrics without data show "—" and the backend's reason.
5. **Now is not history.** The command center separates "right now" numbers from "all time" numbers.
6. **Dense but calm.** Hairline borders, no shadows on panels, 13px body text, tabular numerals.

## Design system

- **Tokens:** `src/index.css`. Surfaces and text follow the shadcn variable set. Each status has four roles: `--x` (text and icon, AA on any surface), `--x-solid` (fills), `--x-tint`, `--x-edge`. Contrast was computed for both themes.
- **Type:** IBM Plex Sans and Plex Mono, self-hosted via `@fontsource`, so the console never depends on a public CDN during an incident.
- **Primitives:** `src/components/ui/` is shadcn/ui (new-york-v4, Tailwind v4, unified `radix-ui`), customised: flat cards, denser buttons and tables, status badge variants.
- **Domain components:** `src/components/sentinel/`.
  - `StatusBadge`, `SeverityBadge`
  - `LifecycleRail` (mini and full)
  - `ConfidenceMeter`, `MetricMeter`
  - `IncidentCard`, `Panel`, `Timestamp`
  - `States` (empty, error, callout, skeleton)
  - `ConfirmDialog`, `LiveIndicator`
- **Derivations:** `src/utils/incident.js` (lifecycle stages, outcomes, action summaries), `utils/posture.js` (the posture shown in the topbar and banner), `utils/labels.js` (backend enums to wording).

## Data

The API layer, hooks and auth from the previous GUI are kept. Additions:

- **One shared SSE connection** (`api/events.js`), with a connection-state indicator.
- **`IncidentFeedContext`:** the newest 50 incidents plus every incident still escalated, so "needs attention" is never under-counted.
- **`ActivityContext`:** activity status; the last watcher snapshot is kept with its timestamp, because the backend reports watchers only while idle.
- **`IncidentActivityChart` / `ResolutionTimeChart`:** built from the real incident list (latest 200).

## Backend notes surfaced during the redesign

Nothing in `sentinel-ai` was changed. Worth knowing:

- The incident list `count` is the page size, not a total. The old GUI's pager relied on it and could never advance. The new list loads a 200-incident window and offers "Load older incidents".
- `/api/activity/status` includes `watchers` only when idle.
- The confidence required at the time an *allowed* action ran is not recorded; only rejected actions carry `required_confidence`.
- Authorization lifetime is not exposed by the API, so the UI says "single-use and short-lived" rather than a number.
- Summary counts (`escalated_incidents`) and performance counts (`escalations`) use different definitions, so they can differ.
- On restart, the backend resumes non-terminal incidents and escalates them as "interrupted by restart".

## Working on it

```bash
npm ci
npm run dev        # http://localhost:5173, uses VITE_API_BASE_URL (see .env.example)
npm run lint
npm run build
```

Adding a component follows shadcn's convention: source lives in `src/components/ui/`, imports use `@/…`, and `components.json` is configured (`tsx: false`). The shadcn CLI needs network access to `ui.shadcn.com`; the components here were copied from the shadcn repository and converted from TSX to JSX.
