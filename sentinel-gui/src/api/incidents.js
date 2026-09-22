import { client } from './client.js'

export function listIncidents({ limit = 50, offset = 0, status = null } = {}) {
  const params = { limit, offset }
  if (status) params.status = status
  return client.get('/api/incidents', { params }).then((r) => r.data)
}

export function getIncident(incidentId) {
  return client.get(`/api/incidents/${incidentId}`).then((r) => r.data)
}

// The document endpoint (routers/incidents.py) is auth-gated like everything
// else in the GUI API, so a plain <a href> can't be used - a browser
// navigation can't carry the Authorization header. Fetch it as a blob
// through the authenticated client instead and open it in a new tab.
/**
 * Asks Sentinel to run a fresh investigation of an escalated incident
 * (POST /api/incidents/{id}/reinvestigate — sentinel-ai/app/routers/reinvestigate.py).
 * The backend endpoint already existed; the console simply never exposed it.
 */
export function reinvestigateIncident(incidentId) {
  return client.post(`/api/incidents/${incidentId}/reinvestigate`).then((r) => r.data)
}

/**
 * Evidence-backed graph view (sentinel-ai app/lifecycle/causal_graph.py).
 * Derived at request time from the same stored incident record — never a
 * separate source of truth, so it can never drift from what the incident
 * page's other tabs already show.
 */
export function getCausalGraph(incidentId) {
  return client.get(`/api/incidents/${incidentId}/causal-graph`).then((r) => r.data)
}

/**
 * What Sentinel's CURRENT rules/decision/risk/policy would conclude from
 * this incident's already-recorded evidence (sentinel-ai
 * app/lifecycle/replay.py). Never triggers a live re-investigation and
 * never executes anything - read-only, like every other incident endpoint.
 */
export function getIncidentReplay(incidentId, { fromScratch = false } = {}) {
  const params = fromScratch ? { from_scratch: true } : {}
  return client.get(`/api/incidents/${incidentId}/replay`, { params }).then((r) => r.data)
}

export async function openIncidentDocument(incidentId) {
  const response = await client.get(`/api/incidents/${incidentId}/document`, {
    responseType: 'blob',
  })
  const url = URL.createObjectURL(response.data)
  window.open(url, '_blank', 'noopener,noreferrer')
  setTimeout(() => URL.revokeObjectURL(url), 30_000)
}
