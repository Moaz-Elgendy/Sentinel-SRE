import { useEffect, useState } from 'react'
import { getLifecyclePhases } from '../api/meta.js'

// The lifecycle vocabulary comes from the backend (GET /api/meta/lifecycle-phases)
// so the console never drifts from the orchestrator. It is static for the life of
// the process, so it is fetched once and shared by every rail on screen.
const FALLBACK = {
  primary_flow_order: [
    'detection',
    'investigation',
    'correlation',
    'root_cause_analysis',
    'remediation_decision',
    'policy_check',
    'autonomous_execution',
    'recovery_validation',
    'documentation',
  ],
  labels: {},
}

let cached = null
let inflight = null

function load() {
  if (cached) return Promise.resolve(cached)
  if (!inflight) {
    inflight = getLifecyclePhases()
      .then((meta) => {
        cached = meta
        return meta
      })
      .catch(() => {
        inflight = null
        return FALLBACK
      })
  }
  return inflight
}

export function usePhaseMeta() {
  const [meta, setMeta] = useState(cached ?? FALLBACK)
  useEffect(() => {
    let cancelled = false
    load().then((loaded) => {
      if (!cancelled) setMeta(loaded)
    })
    return () => {
      cancelled = true
    }
  }, [])
  return meta
}
