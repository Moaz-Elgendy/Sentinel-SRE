import { client } from './client.js'

export function getPerformanceSummary() {
  return client.get('/api/performance/summary').then((r) => r.data)
}
