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
export async function openIncidentDocument(incidentId) {
  const response = await client.get(`/api/incidents/${incidentId}/document`, {
    responseType: 'blob',
  })
  const url = URL.createObjectURL(response.data)
  window.open(url, '_blank', 'noopener,noreferrer')
  setTimeout(() => URL.revokeObjectURL(url), 30_000)
}
