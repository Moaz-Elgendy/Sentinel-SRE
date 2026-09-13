import { useCallback, useEffect, useRef, useState } from 'react'
import { getIncident } from '../api/incidents.js'
import { subscribeToIncidentEvents } from '../api/events.js'

// Slow — SSE (see api/events.js) is what makes this feel live; this interval
// only exists so the view still self-heals if the SSE connection is down
// for a while (proxy issue, token not yet refreshed, etc). Phase A's
// equivalent used a 2.5s poll as its ONLY mechanism; now that a real push
// channel exists, polling only needs to be a safety net.
const FALLBACK_POLL_MS = 10000

/**
 * Live view of one incident: fetches it immediately, re-fetches instantly
 * whenever the real-time event stream reports a change for this incident id
 * (see api/events.js), and otherwise re-fetches on a slow fallback interval
 * so the view still self-heals if the stream is unavailable. Every refetch
 * calls the same real `GET /api/incidents/{id}` Phase A already used — the
 * event is only ever a trigger, never a payload trusted as final state.
 */
export function useLiveIncident(incidentId) {
  const [incident, setIncident] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const incidentIdRef = useRef(incidentId)

  useEffect(() => {
    incidentIdRef.current = incidentId
  })

  const refetch = useCallback(async () => {
    try {
      const result = await getIncident(incidentIdRef.current)
      setIncident(result)
      setError(null)
    } catch (err) {
      setError(err)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    setLoading(true)
    refetch()

    const unsubscribe = subscribeToIncidentEvents((updatedIncidentId) => {
      if (updatedIncidentId === incidentIdRef.current) {
        refetch()
      }
    })

    const timer = setInterval(refetch, FALLBACK_POLL_MS)

    return () => {
      unsubscribe()
      clearInterval(timer)
    }
  }, [incidentId, refetch])

  return { incident, error, loading }
}
