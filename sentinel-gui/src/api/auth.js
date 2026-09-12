import { client } from './client.js'

export function login(username, password) {
  return client.post('/api/auth/login', { username, password }).then((r) => r.data)
}

export function me() {
  return client.get('/api/auth/me').then((r) => r.data)
}
