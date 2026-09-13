import { client } from './client.js'

export function listEnvironments() {
  return client.get('/environments').then((r) => r.data)
}

export function getEnvironment(environmentId) {
  return client.get(`/environments/${environmentId}`).then((r) => r.data)
}

export function testEnvironmentConnection(environmentId) {
  return client.post(`/environments/${environmentId}/test-connection`).then((r) => r.data)
}
