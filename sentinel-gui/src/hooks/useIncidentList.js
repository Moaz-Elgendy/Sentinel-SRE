import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { subscribeToIncidentEvents } from '../api/events.js'
import { extractErrorMessage } from '../api/client.js'
import { listIncidents } from '../api/incidents.js'
import { usePolling } from './usePolling.js'

const WINDOW = 200

const fetchNewest = () => listIncidents({ limit: WINDOW })

/**
 * The incident list's data: the newest 200 incidents (live), plus older pages on
 * demand. Filtering, sorting and paging happen in the browser over this window.
 *
 * Note the backend's list `count` is the size of the page returned, not a total,
 * so "is there more?" is answered by whether the last page came back full.
 */
export function useIncidentList() {
  const newest = usePolling(fetchNewest, { intervalMs: 10000 })
  const { refetch } = newest
  const [older, setOlder] = useState([])
  const [exhausted, setExhausted] = useState(false)
  const [loadingOlder, setLoadingOlder] = useState(false)
  const [olderError, setOlderError] = useState(null)
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

  const newestIncidents = newest.data?.incidents

  const incidents = useMemo(() => {
    const seen = new Set()
    return [...(newestIncidents ?? []), ...older].filter((i) => (seen.has(i.id) ? false : seen.add(i.id)))
  }, [newestIncidents, older])

  const canLoadOlder = !exhausted && (newestIncidents?.length ?? 0) >= WINDOW

  const loadOlder = useCallback(async () => {
    setLoadingOlder(true)
    setOlderError(null)
    try {
      const page = await listIncidents({ limit: WINDOW, offset: WINDOW + older.length })
      setOlder((prev) => [...prev, ...page.incidents])
      if (page.incidents.length < WINDOW) setExhausted(true)
    } catch (err) {
      setOlderError(extractErrorMessage(err, 'Could not load older incidents.'))
    } finally {
      setLoadingOlder(false)
    }
  }, [older.length])

  return {
    incidents,
    loading: newest.loading,
    error: newest.error && !newestIncidents ? newest.error : null,
    refetch,
    updatedAt: newest.updatedAt,
    canLoadOlder,
    loadOlder,
    loadingOlder,
    olderError,
  }
}
