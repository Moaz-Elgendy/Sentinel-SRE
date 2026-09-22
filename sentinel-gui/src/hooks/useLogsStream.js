import { useEffect, useRef, useState } from 'react'
import { listLogs, streamLogs } from '../api/logs.js'

const MAX_DISPLAYED_LINES = 2000

/**
 * Sentinel Logs' data source: an initial `GET /api/logs` query (already-
 * persisted lines matching the current filters), then a live SSE tail that
 * prepends new lines as Sentinel's own logger actually emits them (see
 * app/core/log_capture.py) — no frontend-invented lines, ever.
 *
 * Ordering: newest first, top to bottom, throughout. `GET /api/logs`
 * (app/core/log_capture.py's `query_logs`) already returns most-recent-first
 * — it reads the live file backward, then older rotated backups — so the
 * initial page is used exactly as received, with no client-side reversal.
 * A line arriving live is always newer than everything already loaded, so
 * it is prepended, never appended.
 *
 * `paused`: while true, incoming live lines are buffered but NOT prepended
 * to `lines` — nothing is lost, resuming flushes the buffer. This is a
 * display control only. The buffer fills in arrival order (oldest-buffered
 * first); flushing reverses it before prepending, so the final order is
 * still strictly newest-first once merged with what was already showing.
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
      // bufferRef accumulated oldest-arrival-first; reverse it so the most
      // recently arrived line ends up at index 0, ahead of everything that
      // was already showing.
      setLines((prev) => [...[...bufferRef.current].reverse(), ...prev].slice(0, MAX_DISPLAYED_LINES))
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
        // The API already returns most-recent-first; use it as-is so the
        // newest entry is at the top of the feed.
        setLines(result.logs)
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
        setLines((prev) => [entry, ...prev].slice(0, MAX_DISPLAYED_LINES))
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
