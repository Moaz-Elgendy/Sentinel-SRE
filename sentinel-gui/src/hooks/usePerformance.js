import { getPerformanceSummary } from '../api/performance.js'
import { usePolling } from './usePolling.js'

/** GET /api/performance/summary, polled slowly: these are all-time aggregates. */
export function usePerformance(intervalMs = 30000) {
  return usePolling(getPerformanceSummary, { intervalMs })
}
