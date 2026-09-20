import { client, getToken } from './client.js'

const baseURL = import.meta.env.VITE_API_BASE_URL || window.location.origin

// GET /api/logs?level=&component=&incident_id=&since=&until=&q=&limit= —
// see app/routers/logs.py. Returns the most recent matching lines already
// persisted (before this page was opened).
export function listLogs({ level, component, incidentId, since, until, q, limit = 200 } = {}) {
  const params = {}
  if (level) params.level = level
  if (component) params.component = component
  if (incidentId) params.incident_id = incidentId
  if (since) params.since = since
  if (until) params.until = until
  if (q) params.q = q
  if (limit) params.limit = limit
  return client.get('/api/logs', { params }).then((r) => r.data)
}

/**
 * Live tail via SSE (GET /api/logs/stream — see app/routers/logs.py), same
 * auth-via-query-param approach as api/events.js's subscribeToIncidentEvents
 * and for the identical reason (EventSource cannot set headers).
 *
 * `onLine(entry)` is called with each real, already-redacted log line as it
 * is emitted by Sentinel's own logger (see app/core/log_capture.py) — never
 * a frontend-invented line. Filters are passed straight through to the
 * server so a filtered view doesn't pay for lines it will just discard.
 *
 * Returns a cleanup function; a no-op one if signed out.
 */
export function streamLogs({ level, component, incidentId } = {}, onLine) {
  const token = getToken()
  if (!token) return () => {}

  const params = new URLSearchParams({ token })
  if (level) params.set('level', level)
  if (component) params.set('component', component)
  if (incidentId) params.set('incident_id', incidentId)

  const url = `${baseURL}/api/logs/stream?${params.toString()}`
  const source = new EventSource(url)

  source.addEventListener('log', (event) => {
    try {
      onLine(JSON.parse(event.data))
    } catch {
      // Malformed line — skip it, the rest of the stream is unaffected.
    }
  })

  return () => source.close()
}
