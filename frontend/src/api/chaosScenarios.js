import axios from 'axios'
import { extractErrorMessage } from './client'

const baseURL = import.meta.env.VITE_SENTINEL_API_BASE_URL ?? '/api/sentinel'

export const chaosClient = axios.create({ baseURL })

function authHeaders(token) {
  return { 'X-Chaos-Token': token }
}

export async function listChaosScenarios(token) {
  const response = await chaosClient.get('/chaos-scenarios', {
    headers: authHeaders(token),
  })
  return response.data
}

export async function runChaosScenario(id, { token, namespace, autoRollback }) {
  const response = await chaosClient.post(
    `/chaos-scenarios/${id}/runs`,
    { namespace, auto_rollback: autoRollback },
    { headers: authHeaders(token) }
  )
  return response.data
}

export async function getChaosRun(commandId, token) {
  const response = await chaosClient.get(`/chaos-scenarios/runs/${commandId}`, {
    headers: authHeaders(token),
  })
  return response.data
}

export function extractChaosError(error) {
  return extractErrorMessage(error, 'Could not run the chaos scenario.')
}
