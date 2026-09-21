import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { getActivityStatus } from '../api/activity.js'
import { subscribeToIncidentEvents } from '../api/events.js'
import { usePolling } from '../hooks/usePolling.js'

/**
 * GET /api/activity/status, shared. Carries what the summary endpoint does not:
 * every concurrently active incident, the LLM reasoner's health, and (only while
 * Sentinel is idle) the live connectivity of its watchers.
 *
 * The backend reports `watchers` only in the idle state, so the last snapshot is
 * kept together with when it was taken — the UI can then say "checked 3m ago"
 * instead of going blank the moment an incident starts.
 */
const ActivityContext = createContext(null)

export function ActivityProvider({ children }) {
  const [watchers, setWatchers] = useState(null)
  // Remember the last watcher snapshot the moment it arrives (see the note above).
  const fetchStatus = useCallback(async () => {
    const result = await getActivityStatus()
    if (result.watchers) setWatchers({ items: result.watchers, at: Date.now() })
    return result
  }, [])
  const poll = usePolling(fetchStatus, { intervalMs: 8000 })
  const { data: status, refetch } = poll
  const timerRef = useRef(null)

  useEffect(() => {
    const unsubscribe = subscribeToIncidentEvents(() => {
      clearTimeout(timerRef.current)
      timerRef.current = setTimeout(refetch, 250)
    })
    return () => {
      clearTimeout(timerRef.current)
      unsubscribe()
    }
  }, [refetch])

  const value = useMemo(
    () => ({
      status,
      watchers,
      error: poll.error && !status ? poll.error : null,
      loading: poll.loading,
      refetch,
    }),
    [status, watchers, poll.error, poll.loading, refetch]
  )
  return <ActivityContext.Provider value={value}>{children}</ActivityContext.Provider>
}

export function useActivity() {
  const ctx = useContext(ActivityContext)
  if (!ctx) throw new Error('useActivity must be used within an ActivityProvider')
  return ctx
}
