import { createContext, useCallback, useContext, useEffect, useMemo, useRef } from 'react'
import { subscribeToIncidentEvents } from '../api/events.js'
import { listIncidents } from '../api/incidents.js'
import { usePolling } from '../hooks/usePolling.js'

/**
 * The console's shared picture of incidents. Two real queries, one owner:
 *
 *   recent     the newest incidents (covers everything in flight, plus recent history)
 *   escalated  every incident still waiting on a human, however old — the newest-N
 *              window alone could hide a stale escalation, and "needs attention"
 *              must never be under-counted.
 *
 * Both are refreshed instantly on any `incident_updated` SSE event and otherwise
 * on a slow interval, so the sidebar badges, the command center, the incident
 * list and the command palette always agree with each other.
 */
const RECENT_LIMIT = 50
const EVENT_DEBOUNCE_MS = 250

const IncidentFeedContext = createContext(null)

const fetchRecent = () => listIncidents({ limit: RECENT_LIMIT })
const fetchEscalated = () => listIncidents({ limit: RECENT_LIMIT, status: 'escalated' })

export function IncidentFeedProvider({ children }) {
  const recentPoll = usePolling(fetchRecent, { intervalMs: 10000 })
  const escalatedPoll = usePolling(fetchEscalated, { intervalMs: 15000 })
  const refetchRecent = recentPoll.refetch
  const refetchEscalated = escalatedPoll.refetch
  const timerRef = useRef(null)

  const refetch = useCallback(() => {
    refetchRecent()
    refetchEscalated()
  }, [refetchRecent, refetchEscalated])

  useEffect(() => {
    const unsubscribe = subscribeToIncidentEvents(() => {
      clearTimeout(timerRef.current)
      timerRef.current = setTimeout(refetch, EVENT_DEBOUNCE_MS)
    })
    return () => {
      clearTimeout(timerRef.current)
      unsubscribe()
    }
  }, [refetch])

  const recent = recentPoll.data?.incidents
  const escalatedRaw = escalatedPoll.data?.incidents

  const value = useMemo(() => {
    const recentList = recent ?? []
    const awaiting = (escalatedRaw ?? recentList.filter((i) => i.escalated)).filter((i) => i.escalated !== false)
    const active = recentList.filter((i) => !['resolved', 'escalated', 'auto_resolved'].includes(i.status))
    return {
      recent: recentList,
      active,
      awaiting,
      loading: recentPoll.loading,
      error: recentPoll.error && !recent ? recentPoll.error : null,
      updatedAt: recentPoll.updatedAt,
      refetch,
    }
  }, [recent, escalatedRaw, recentPoll.loading, recentPoll.error, recentPoll.updatedAt, refetch])

  return <IncidentFeedContext.Provider value={value}>{children}</IncidentFeedContext.Provider>
}

export function useIncidentFeed() {
  const ctx = useContext(IncidentFeedContext)
  if (!ctx) throw new Error('useIncidentFeed must be used within an IncidentFeedProvider')
  return ctx
}
