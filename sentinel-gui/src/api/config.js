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

export function getRcaConfig() {
  return client.get('/api/config/rca').then((r) => r.data)
}

export function previewRcaChange(changes) {
  return client.post('/api/config/rca/preview', { changes }).then((r) => r.data)
}

export function applyRcaChange(changes, reason) {
  return client.post('/api/config/rca/apply', { changes, reason: reason || null }).then((r) => r.data)
}

export function getRemediationConfig() {
  return client.get('/api/config/remediation').then((r) => r.data)
}

export function previewRemediationChange(changes) {
  return client.post('/api/config/remediation/preview', { changes }).then((r) => r.data)
}

export function applyRemediationChange(changes, reason) {
  return client.post('/api/config/remediation/apply', { changes, reason: reason || null }).then((r) => r.data)
}

export function getConfigHistory({ category = null, limit = 100, offset = 0 } = {}) {
  const params = { limit, offset }
  if (category) params.category = category
  return client.get('/api/config/history', { params }).then((r) => r.data.history)
}

export function restoreConfigChange(changeId) {
  return client.post(`/api/config/history/${changeId}/restore`).then((r) => r.data)
}
