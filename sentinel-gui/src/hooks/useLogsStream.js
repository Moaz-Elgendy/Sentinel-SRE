import { useEffect, useRef, useState } from 'react'
import { listLogs, streamLogs } from '../api/logs.js'

const MAX_DISPLAYED_LINES = 2000

/**
 * Sentinel Logs' data source: an initial `GET /api/logs` query (already-
 * persisted lines matching the current filters), then a live SSE tail that
 * appends new lines as Sentinel's own logger actually emits them (see
 * app/core/log_capture.py) — no frontend-invented lines, ever.
 *
 * `paused`: while true, incoming live lines are buffered but NOT appended
 * to `lines` — nothing is lost, resuming flushes the buffer. This is a
 * display control only.
 *
 * `clearView()`: empties what is DISPLAYED. Deliberately does not, and
 * cannot, touch the persisted file on the backend — there is no delete
 * endpoint (see routers/logs.py's module docstring) specifically so this
 * button can never be mistaken for deleting the audit trail.
 */
export function useLogsStream({ level, component, incidentId, q } = {}) {
  const [lines, setLines] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [connected, setConnected] = useState(false)
  const [paused, setPaused] = useState(false)
  const [pendingCount, setPendingCount] = useState(0)
  const pausedRef = useRef(paused)
  const bufferRef = useRef([])

  useEffect(() => {
    pausedRef.current = paused
    if (!paused && bufferRef.current.length > 0) {
      setLines((prev) => [...prev, ...bufferRef.current].slice(-MAX_DISPLAYED_LINES))
      bufferRef.current = []
      setPendingCount(0)
    }
  }, [paused])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)

    listLogs({ level, component, incidentId, q, limit: 200 })
      .then((result) => {
        if (cancelled) return
        // The API returns most-recent-first; the feed reads top-to-bottom
        // oldest-first, same convention as the incident audit timeline.
        setLines([...result.logs].reverse())
      })
      .catch((err) => {
        if (!cancelled) setError(err)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    bufferRef.current = []
    setPendingCount(0)

    const unsubscribe = streamLogs({ level, component, incidentId }, (entry) => {
      if (q && !JSON.stringify(entry).toLowerCase().includes(q.toLowerCase())) return
      if (pausedRef.current) {
        bufferRef.current = [...bufferRef.current, entry].slice(-MAX_DISPLAYED_LINES)
        setPendingCount(bufferRef.current.length)
      } else {
        setLines((prev) => [...prev, entry].slice(-MAX_DISPLAYED_LINES))
      }
    })
    setConnected(true)

    return () => {
      cancelled = true
      unsubscribe()
      setConnected(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [level, component, incidentId, q])

  function clearView() {
    setLines([])
  }

  return {
    lines,
    loading,
    error,
    connected,
    paused,
    setPaused,
    pendingCount,
    clearView,
  }
}
