import { getToken } from './client.js'

const baseURL = import.meta.env.VITE_API_BASE_URL || window.location.origin

/**
 * The real-time event stream (sentinel-ai's GET /api/events — see
 * app/routers/events.py). Auth is via a `?token=` query param rather than the
 * usual Authorization header, because the browser's native `EventSource`
 * cannot set custom request headers (same JWT, same secret, same expiry).
 *
 * ONE shared connection. Several parts of the console listen at once (the
 * incident feed, the activity status, an open incident, the topbar's live
 * indicator); each `subscribeToIncidentEvents()` call is fanned out from a
 * single EventSource instead of opening its own. The connection opens with the
 * first subscriber and closes shortly after the last one leaves (the short
 * grace period avoids churn on route changes and React StrictMode remounts).
 *
 * `onIncidentEvent(incidentId, payload)` fires for every `incident_updated`
 * event. Treat it as a signal to re-fetch the real record, never as final
 * state — `payload` is the event as published by the orchestrator and carries
 * the `message` just recorded for that phase transition, `alertname`,
 * `severity`, `phase` and `status`.
 */
const CLOSE_GRACE_MS = 1500

let source = null
let closeTimer = null
let streamState = 'idle' // idle | connecting | open | reconnecting | closed
const incidentListeners = new Set()
const stateListeners = new Set()

function setState(next) {
  if (next === streamState) return
  streamState = next
  stateListeners.forEach((listener) => listener(next))
}

function openConnection() {
  const token = getToken()
  if (!token) return
  const url = `${baseURL}/api/events?token=${encodeURIComponent(token)}`
  source = new EventSource(url)
  setState('connecting')

  source.onopen = () => setState('open')
  source.onerror = () => {
    // EventSource retries on its own while readyState is CONNECTING.
    setState(source && source.readyState === EventSource.CLOSED ? 'closed' : 'reconnecting')
  }
  source.addEventListener('incident_updated', (event) => {
    try {
      const payload = JSON.parse(event.data)
      if (payload.incident_id) {
        incidentListeners.forEach((listener) => listener(payload.incident_id, payload))
      }
    } catch {
      // A malformed frame is ignored; the next poll/refetch self-heals.
    }
  })
}

function closeIfUnused() {
  if (incidentListeners.size > 0 || !source) return
  source.close()
  source = null
  setState('idle')
}

export function subscribeToIncidentEvents(onIncidentEvent) {
  if (!getToken()) return () => {}
  clearTimeout(closeTimer)
  incidentListeners.add(onIncidentEvent)
  if (!source) openConnection()

  return () => {
    incidentListeners.delete(onIncidentEvent)
    clearTimeout(closeTimer)
    closeTimer = setTimeout(closeIfUnused, CLOSE_GRACE_MS)
  }
}

/** Connection state of the shared stream, for the topbar's live indicator. */
export function getStreamState() {
  return streamState
}

export function subscribeToStreamState(listener) {
  stateListeners.add(listener)
  return () => stateListeners.delete(listener)
}
