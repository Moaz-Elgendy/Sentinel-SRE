import { getToken } from './client.js'

const baseURL = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8080'

/**
 * Opens the real-time event stream (sentinel-ai's GET /api/events — see
 * app/routers/events.py). Auth is via a `?token=` query param rather than
 * the usual Authorization header, because the browser's native
 * `EventSource` cannot set custom request headers — see that router's
 * module docstring for why this is safe (same JWT, same secret, same
 * expiry, just a different transport for this one endpoint).
 *
 * `onIncidentEvent(incidentId)` is called for every `incident_updated`
 * event — it receives ONLY the incident id, never incident state, because
 * the event is a signal to re-fetch the real record via the existing
 * `GET /api/incidents/{id}`, not a payload to trust as-is (see
 * usePolling.js and IncidentDetailPage.jsx, which is Phase A's mechanism
 * for turning that signal into an actual update).
 *
 * Returns a cleanup function. If there is no token (signed out) this is a
 * no-op that returns a no-op cleanup, rather than opening an unauthenticated
 * connection that will just 401 forever.
 */
export function subscribeToIncidentEvents(onIncidentEvent) {
  const token = getToken()
  if (!token) return () => {}

  const url = `${baseURL}/api/events?token=${encodeURIComponent(token)}`
  const source = new EventSource(url)

  source.addEventListener('incident_updated', (event) => {
    try {
      const payload = JSON.parse(event.data)
      if (payload.incident_id) onIncidentEvent(payload.incident_id)
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
