import { client } from './client.js'

export function authorizeIncident(incidentId, { action, namespace, deployment, replicas, targetRevision, service }) {
  return client
    .post(`/api/incidents/${incidentId}/authorize`, {
      action,
      namespace: namespace || null,
      deployment: deployment || null,
      replicas: replicas ?? null,
      target_revision: targetRevision ?? null,
      service: service || null,
    })
    .then((r) => r.data)
}

export function listAuthorizations(incidentId) {
  return client.get(`/api/incidents/${incidentId}/authorizations`).then((r) => r.data.authorizations)
}

// ---------------------------------------------------------------------------
// Deep Investigation proposals (sentinel-ai's routers/authorizations.py —
// same shape as the known-action endpoints above, for a structured,
// LLM-proposed novel action rather than one of the four RemediationActions).
// ---------------------------------------------------------------------------

export function listDeepProposals(incidentId) {
  return client.get(`/api/incidents/${incidentId}/deep-proposals`).then((r) => r.data.deep_proposals)
}

/** Authorizes exactly one SUGGESTED proposal. No parameters to pick — the
 * proposal already names its own action_type and target; that structure is
 * the whole point (see DeepRemediationProposal's docstring). */
export function authorizeDeepProposal(incidentId, proposalId) {
  return client.post(`/api/incidents/${incidentId}/deep-proposals/${proposalId}/authorize`).then((r) => r.data)
}
