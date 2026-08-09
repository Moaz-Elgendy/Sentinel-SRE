import { client } from './client'

export function listServices() {
  return client.get('/api/services').then((res) => res.data)
}

export function getService(serviceId) {
  return client.get(`/api/services/${serviceId}`).then((res) => res.data)
}
