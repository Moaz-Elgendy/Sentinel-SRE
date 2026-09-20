import { getToken } from './client.js'

// Same default as client.js — empty (same-origin) unless VITE_API_BASE_URL
// was overridden at build time. EventSource needs a full URL rather than
// axios's baseURL convenience, so build it against window.location when
// baseURL is empty.
const baseURL = import.meta.env.VITE_API_BASE_URL || window.location.origin

/**
 * Opens the real-time event stream (sentinel-ai's GET /api/events — see
 * app/routers/events.py). Auth is via a `?token=` query param rather than
 * the usual Authorization header, because the browser's native
 * `EventSource` cannot set custom request headers — see that router's
 * module docstring for why this is safe (same JWT, same secret, same
 * expiry, just a different transport for this one endpoint).
 *
 * `onIncidentEvent(incidentId, payload)` is called for every `incident_updated`
 * event. `incidentId` is the only part any existing caller should treat as
 * a signal to re-fetch the real record via `GET /api/incidents/{id}` — see
 * usePolling.js and IncidentDetailPage.jsx. `payload` is the full published
 * event (see orchestrator.py's `_persist`) and additionally carries the
 * real `message` just recorded for that phase transition, `alertname`,
 * `severity`, `phase` and `status` — useful for a live feed (Sentinel Live)
 * that wants to render something immediately without waiting for the
 * incident to be re-fetched, while still never being the ONLY source of
 * truth for that incident's state.
 */
export function subscribeToIncidentEvents(onIncidentEvent) {
  const token = getToken()
  if (!token) return () => {}

  const url = `${baseURL}/api/events?token=${encodeURIComponent(token)}`
  const source = new EventSource(url)

  source.addEventListener('incident_updated', (event) => {
    try {
      const payload = JSON.parse(event.data)
      if (payload.incident_id) onIncidentEvent(payload.incident_id, payload)
    } catch {
      // Malformed event — ignore it; the next poll still catches up.
    }
  })

  // EventSource auto-reconnects on a dropped connection by default; we
  // don't need to handle `onerror` ourselves for that. If the token itself
  // is rejected (401), the connection will keep retrying and keep failing —
  // Phase A's polling fallback (usePolling) is what keeps the GUI correct
  // either way, so this is a silent, low-priority failure mode by design.

  return () => source.close()
}
