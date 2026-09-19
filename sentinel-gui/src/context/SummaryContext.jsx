import { createContext, useContext } from 'react'
import { getDashboardSummary } from '../api/dashboard.js'
import { usePolling } from '../hooks/usePolling.js'

// One poll of GET /api/dashboard/summary shared by the topbar, sidebar badge,
// dashboard and environment page. Previously each of those polled on its own
// (up to three concurrent requests for the same endpoint).
const SummaryContext = createContext(null)

export function SummaryProvider({ children }) {
  const poll = usePolling(getDashboardSummary, { intervalMs: 8000 })
  return <SummaryContext.Provider value={poll}>{children}</SummaryContext.Provider>
}

export function useSummary() {
  const ctx = useContext(SummaryContext)
  if (!ctx) throw new Error('useSummary must be used within a SummaryProvider')
  return ctx
}
