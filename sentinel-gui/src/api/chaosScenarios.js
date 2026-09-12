import { client } from './client.js'

// The chaos-scenarios endpoints (routers/chaos_scenarios.py) are NOT part
// of the admin-JWT-gated GUI API surface — they predate this GUI and use
// their own pre-existing shared-secret header (X-Chaos-Token), returning
// 404 rather than 401 on a wrong/missing token by design. This module talks
// to that endpoint exactly as it already exists; nothing about it changes
// for the GUI. The token is kept in sessionStorage only (cleared when the
// browser tab closes), separately from the admin JWT in localStorage — it
// is a different kind of secret with a different lifetime.
const CHAOS_TOKEN_KEY = 'sentinel_gui_chaos_token'

export function getChaosToken() {
  return sessionStorage.getItem(CHAOS_TOKEN_KEY)
}

export function setChaosToken(token) {
  if (token) {
    sessionStorage.setItem(CHAOS_TOKEN_KEY, token)
  } else {
    sessionStorage.removeItem(CHAOS_TOKEN_KEY)
  }
}

function chaosHeaders() {
  const token = getChaosToken()
  return token ? { 'X-Chaos-Token': token } : {}
}

export function listScenarios() {
  return client.get('/api/sentinel/chaos-scenarios', { headers: chaosHeaders() }).then((r) => r.data)
}

export function runScenario(scenarioId, { namespace = 'citizen-portal', autoRollback = false } = {}) {
  return client
    .post(
      `/api/sentinel/chaos-scenarios/${scenarioId}/runs`,
      { namespace, auto_rollback: autoRollback },
      { headers: chaosHeaders() }
    )
    .then((r) => r.data)
}

export function getRunStatus(commandId) {
  return client
    .get(`/api/sentinel/chaos-scenarios/runs/${commandId}`, { headers: chaosHeaders() })
    .then((r) => r.data)
}
