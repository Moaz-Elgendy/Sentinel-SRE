import { useEffect, useRef, useState } from 'react'

/**
 * Polls `fetchFn` every `intervalMs` while `enabled` is true, and once
 * immediately on mount/whenever `enabled` flips true. Returns the latest
 * successful result, the latest error, and a `loading` flag for the very
 * first fetch only (subsequent polls update `data` in place rather than
 * flashing a loading state, since the point is a live-feeling view).
 *
 * This exists because Phase A of the Sentinel GUI has no push/SSE channel
 * yet (see the approved GUI plan, Phase B) — every "live" view in Phase A
 * is short-interval polling against the real, already-persisted incident
 * record (Orchestrator._persist() writes after every phase), which is
 * genuine data, just not push-delivered. Swapping this hook's internals for
 * an EventSource-driven version later should not require call-site changes.
 */
export function usePolling(fetchFn, { intervalMs = 4000, enabled = true } = {}) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const fetchFnRef = useRef(fetchFn)

  useEffect(() => {
    fetchFnRef.current = fetchFn
  })

  useEffect(() => {
    if (!enabled) return undefined
    let cancelled = false
    let timer

    async function tick() {
      try {
        const result = await fetchFnRef.current()
        if (!cancelled) {
          setData(result)
          setError(null)
        }
      } catch (err) {
        if (!cancelled) setError(err)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    tick()
    timer = setInterval(tick, intervalMs)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [intervalMs, enabled])

  return { data, error, loading }
}
