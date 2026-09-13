import { client } from './client.js'

export function submitDiagnosisFeedback(incidentId, { correct, actualRootCause = null, note = null }) {
  return client
    .post(`/api/incidents/${incidentId}/feedback/diagnosis`, {
      correct,
      actual_root_cause: actualRootCause,
      note,
    })
    .then((r) => r.data)
}

export function submitRemediationFeedback(incidentId, { useful, suggestedAction = null, note = null }) {
  return client
    .post(`/api/incidents/${incidentId}/feedback/remediation`, {
      useful,
      suggested_action: suggestedAction,
      note,
    })
    .then((r) => r.data)
}

export function listIncidentFeedback(incidentId) {
  return client.get(`/api/incidents/${incidentId}/feedback`).then((r) => r.data.feedback)
}
