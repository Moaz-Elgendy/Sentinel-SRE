import { client } from './client.js'

export function getDashboardSummary() {
  return client.get('/api/dashboard/summary').then((r) => r.data)
}
