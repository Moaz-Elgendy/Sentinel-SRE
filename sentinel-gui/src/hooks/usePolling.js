import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * Polls `fetchFn` every `intervalMs` while `enabled` is true, and once
 * immediately on mount/whenever `enabled` flips true.
 *
 * Returns:
 *   data       latest successful result (kept on screen if a later poll fails)
 *   error      latest error, cleared by the next success
 *   loading    true only for the very first fetch
 *   busy       true while a *requested* fetch is in flight (filter change or
 *              refetch) — background polls don't set it, so live views don't flicker
 *   updatedAt  ms timestamp of the last successful fetch
 *   refetch    run a fetch now
 *
 * `resetKey`: when it changes (e.g. the filter or page the caller is showing)
 * the hook fetches immediately instead of waiting for the next interval tick.
 * Without this a filter change could show stale rows for a full poll interval.
 *
 * This is short-interval polling against real, already-persisted records —
 * genuine data, just not push-delivered (incident detail uses SSE; see
 * useLiveIncident).
 */
export function usePolling(fetchFn, { intervalMs = 4000, enabled = true, resetKey = '' } = {}) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [updatedAt, setUpdatedAt] = useState(null)
  const fetchFnRef = useRef(fetchFn)
  const runRef = useRef(null)
  const firstRunRef = useRef(true)

  useEffect(() => {
    fetchFnRef.current = fetchFn
  })

  useEffect(() => {
    if (!enabled) return undefined
    let cancelled = false

    async function tick(showBusy) {
      if (showBusy) setBusy(true)
      try {
        const result = await fetchFnRef.current()
        if (!cancelled) {
          setData(result)
          setError(null)
          setUpdatedAt(Date.now())
        }
      } catch (err) {
        if (!cancelled) setError(err)
      } finally {
        if (!cancelled) {
          setLoading(false)
          setBusy(false)
        }
      }
    }

    runRef.current = tick
    tick(!firstRunRef.current)
    firstRunRef.current = false
    const timer = setInterval(() => tick(false), intervalMs)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [intervalMs, enabled, resetKey])

  const refetch = useCallback(() => runRef.current?.(true), [])

  return { data, error, loading, busy, updatedAt, refetch }
}
