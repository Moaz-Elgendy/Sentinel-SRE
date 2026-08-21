import { client } from './client'

export function register(payload) {
  // payload: { full_name, national_id, email, phone, password }
  return client.post('/api/auth/register', payload).then((res) => res.data)
}

export function login(payload) {
  // payload: { email, password } -> { access_token, token_type }
  return client.post('/api/auth/login', payload).then((res) => res.data)
}

export function getCurrentCitizen() {
  return client.get('/api/auth/me').then((res) => res.data)
}
