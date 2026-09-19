import { useEffect, useState } from 'react'

/** Re-renders the caller every `intervalMs` so relative-time labels stay honest. */
export function useNow(intervalMs = 5000) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), intervalMs)
    return () => clearInterval(timer)
  }, [intervalMs])
  return now
}
