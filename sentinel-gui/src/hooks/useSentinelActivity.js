import { useEffect, useRef, useState } from 'react'
import { subscribeToIncidentEvents } from '../api/events.js'
import { getActivityStatus } from '../api/activity.js'
import { usePolling } from './usePolling.js'

const FALLBACK_POLL_MS = 6000
const MAX_FEED_ENTRIES = 100

/**
 * Sentinel Live's data source. Two things, both genuinely real:
 *
 *   1. `status` — the current idle/monitoring/investigating state, from
 *      `GET /api/activity/status` (see app/routers/activity.py). Polled on
 *      a slow interval as a self-healing fallback, and refetched instantly
 *      whenever an `incident_updated` SSE event arrives — same pattern as
 *      useLiveIncident.js.
 *
 *   2. `feed` — a rolling window of individual activity lines, built from
 *      the SAME `incident_updated` events (now carrying the real timeline
 *      message — see orchestrator.py's `_persist`), not invented here.
 *      Seeded from `status.recent_timeline` so a page opened mid-incident
 *      shows history immediately rather than an empty feed waiting for the
 *      next event.
 */
export function useSentinelActivity() {
  const { data: status, error, loading, busy, updatedAt, refetch } = usePolling(getActivityStatus, {
    intervalMs: FALLBACK_POLL_MS,
  })
  const [feed, setFeed] = useState([])
  const seededIncidentRef = useRef(null)

  // Seed the feed from backfilled history the moment an incident becomes
  // (or already is, on first load) active — once per incident, so it
  // doesn't stomp on lines the live stream has already appended.
  useEffect(() => {
    if (status?.state !== 'investigating') return
    if (seededIncidentRef.current === status.incident_id) return
    seededIncidentRef.current = status.incident_id

    const seeded = (status.recent_timeline ?? []).map((event) => ({
      incident_id: status.incident_id,
      alertname: status.alertname,
      severity: status.severity,
      phase: event.phase,
      message: event.message,
      at: event.at,
    }))
    setFeed(seeded.slice(-MAX_FEED_ENTRIES))
  }, [status])

  useEffect(() => {
    const unsubscribe = subscribeToIncidentEvents((_incidentId, payload) => {
      refetch()
      setFeed((prev) => [
        ...prev,
        {
          incident_id: payload.incident_id,
          alertname: payload.alertname,
          severity: payload.severity,
          phase: payload.phase,
          message: payload.message,
          at: payload.published_at,
        },
      ].slice(-MAX_FEED_ENTRIES))
    })
    return unsubscribe
  }, [refetch])

  return { status, feed, error, loading, busy, updatedAt, refetch }
}
