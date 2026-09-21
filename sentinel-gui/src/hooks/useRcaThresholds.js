import { useEffect, useState } from 'react'
import { getRcaConfig } from '../api/config.js'

let cached = null

/**
 * The limits Sentinel's diagnosis and recovery validation use (max error rate,
 * p95, CPU, memory). Evidence is only meaningful next to them. Best effort:
 * if the config cannot be read, evidence still renders, just without limits.
 */
export function useRcaThresholds() {
  const [limits, setLimits] = useState(cached)
  useEffect(() => {
    let cancelled = false
    getRcaConfig()
      .then((body) => {
        cached = body.current
        if (!cancelled) setLimits(body.current)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [])
  return limits
}
