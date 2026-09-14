import { client } from './client.js'

export function getPolicyConfig() {
  return client.get('/api/config/policy').then((r) => r.data)
}

export function previewPolicyChange(changes) {
  return client.post('/api/config/policy/preview', { changes }).then((r) => r.data)
}

export function applyPolicyChange(changes, reason) {
  return client.post('/api/config/policy/apply', { changes, reason: reason || null }).then((r) => r.data)
}

export function getConfigHistory({ category = null, limit = 100, offset = 0 } = {}) {
  const params = { limit, offset }
  if (category) params.category = category
  return client.get('/api/config/history', { params }).then((r) => r.data.history)
}

export function restoreConfigChange(changeId) {
  return client.post(`/api/config/history/${changeId}/restore`).then((r) => r.data)
}
