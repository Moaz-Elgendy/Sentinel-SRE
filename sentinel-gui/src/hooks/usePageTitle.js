import { useEffect } from 'react'

/** Keeps the browser tab / history entry meaningful as the user navigates. */
export function usePageTitle(title) {
  useEffect(() => {
    const previous = document.title
    document.title = title ? `${title} · Sentinel` : 'Sentinel SRE Control Center'
    return () => {
      document.title = previous
    }
  }, [title])
}
