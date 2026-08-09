import { client } from './client'

export function listRequests() {
  return client.get('/api/requests').then((res) => res.data)
}

export function getRequest(requestId) {
  return client.get(`/api/requests/${requestId}`).then((res) => res.data)
}

export function createRequest(serviceId) {
  return client.post('/api/requests', { service_id: serviceId }).then((res) => res.data)
}
