import { client } from './client.js'

// GET /api/activity/status — see app/routers/activity.py. Real idle/
// monitoring/investigating state, never a client-side guess.
export function getActivityStatus() {
  return client.get('/api/activity/status').then((r) => r.data)
}
