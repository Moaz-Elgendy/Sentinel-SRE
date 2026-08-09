import { client } from './client'

export function getProfile() {
  return client.get('/api/profile').then((res) => res.data)
}

export function updateProfile(payload) {
  // payload: { full_name?, phone? }
  return client.put('/api/profile', payload).then((res) => res.data)
}
