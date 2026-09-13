import { client } from './client.js'

export function listActions({ limit = 100, offset = 0, action = null, app = null, incidentId = null } = {}) {
  const params = { limit, offset }
  if (action) params.action = action
  if (app) params.app = app
  if (incidentId) params.incident_id = incidentId
  return client.get('/api/actions', { params }).then((r) => r.data)
}
