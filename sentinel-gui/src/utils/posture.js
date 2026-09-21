/**
 * Posture: the single answer to "what is the state of my environment, and do I
 * need to do anything?" It is derived only from real signals — the dashboard
 * summary, the shared incident feed, and the activity status — and it is the
 * same on the topbar chip and the command center banner so they can never
 * disagree.
 *
 * Priority (highest first): needs a human → services degraded/critical →
 * incident in flight → nothing measurable → operational.
 */
export function derivePosture({ summary, summaryError, awaiting = [], active = [], activityStatus, loading = false }) {
  // Never assert a state we haven't measured yet: until the first summary and incident
  // feed arrive, say so instead of briefly claiming "no active incidents".
  if (loading) {
    return {
      key: 'loading',
      tone: 'neutral',
      icon: 'unknown',
      chip: 'Checking…',
      headline: 'Checking the environment…',
      detail: 'Reading incidents and service health.',
      focus: null,
      count: 0,
    }
  }

  const health = summary?.system_health?.status
  const services = summary?.system_health?.services ?? []
  const unhealthy = services.filter((s) => s.healthy === false)

  if (awaiting.length > 0) {
    const oldest = awaiting.reduce((a, b) => (a.updated_at <= b.updated_at ? a : b))
    return {
      key: 'attention',
      tone: 'warn',
      icon: 'hand',
      chip: `${awaiting.length} need${awaiting.length === 1 ? 's' : ''} you`,
      headline:
        awaiting.length === 1 ? 'One incident needs a human' : `${awaiting.length} incidents need a human`,
      detail:
        awaiting.length === 1
          ? 'Sentinel could not act safely on its own and is waiting for your decision.'
          : 'Sentinel could not act safely on these on its own and is waiting for your decision.',
      focus: awaiting.length === 1 ? awaiting[0] : oldest,
      count: awaiting.length,
    }
  }

  if (health === 'critical' || unhealthy.length > 0) {
    return {
      key: 'degraded',
      tone: health === 'critical' ? 'bad' : 'warn',
      icon: 'alert',
      chip: health === 'critical' ? 'Critical' : 'Degraded',
      headline:
        unhealthy.length > 0
          ? `${unhealthy.length} service${unhealthy.length === 1 ? ' is' : 's are'} unhealthy`
          : 'Services are degraded',
      detail: unhealthy.length > 0 ? unhealthy.map((s) => s.name).join(', ') : 'Check the service list below.',
      focus: null,
    }
  }

  if (active.length > 0) {
    const most = [...active].sort((a, b) => b.updated_at - a.updated_at)[0]
    return {
      key: 'active',
      tone: 'info',
      icon: 'activity',
      chip: `${active.length} in progress`,
      headline:
        active.length === 1 ? 'Sentinel is handling an incident' : `Sentinel is handling ${active.length} incidents`,
      detail: 'Investigating, remediating and validating on its own. No action needed yet.',
      focus: most,
      count: active.length,
    }
  }

  if (summaryError && !summary) {
    return {
      key: 'unavailable',
      tone: 'neutral',
      icon: 'unknown',
      chip: 'Status unavailable',
      headline: 'Sentinel status is unavailable',
      detail: 'The console could not reach the Sentinel API.',
      focus: null,
    }
  }

  if (health === 'operational') {
    return {
      key: 'operational',
      tone: 'ok',
      icon: 'ok',
      chip: 'Operational',
      headline: 'All systems operational',
      detail: 'No active incidents. Sentinel is watching.',
      focus: null,
    }
  }

  // Health "unknown" is honest: e.g. Kubernetes is unreachable, so service
  // health cannot be measured. That must not be dressed up as green.
  return {
    key: 'unknown',
    tone: 'neutral',
    icon: 'unknown',
    chip: 'Health unknown',
    headline: 'No active incidents, but health cannot be confirmed',
    detail: activityStatus?.state === 'degraded' ? (activityStatus.message ?? null) : 'Service health is not measurable right now.',
    focus: null,
  }
}
