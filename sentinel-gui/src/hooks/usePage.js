import { useState } from 'react'

/**
 * Page index that returns to 0 whenever `resetKey` changes (a filter, a search),
 * derived during render rather than through an effect so there is never a frame
 * showing a stale page of a different result set.
 */
export function usePage(resetKey) {
  const [state, setState] = useState({ key: resetKey, page: 0 })
  const page = state.key === resetKey ? state.page : 0
  const setPage = (next) => setState({ key: resetKey, page: next })
  return [page, setPage]
}
