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
