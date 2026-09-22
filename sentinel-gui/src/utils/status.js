import { titleCase } from './format.js'

// One vocabulary for every status shown in the console. Each state maps to a
// semantic tone, so "escalated" looks the same on the dashboard, in a table
// and in the incident header.
//
//   ok       healthy, resolved, recovered, succeeded
//   info     in progress: investigating, remediating, validating
//   warn     degraded, escalated (needs a human)
//   bad      critical, failed, down
//   neutral  unknown, idle, cancelled
const TONE_BY_STATUS = {
  // system / service health
  operational: 'ok',
  healthy: 'ok',
  monitoring: 'ok',
  degraded: 'warn',
  critical: 'bad',
  down: 'bad',
  unknown: 'neutral',
  null: 'neutral',
  true: 'ok',
  false: 'bad',

  // incident lifecycle
  open: 'info',
  investigating: 'info',
  remediating: 'info',
  validating: 'info',
  resolved: 'ok',
  auto_resolved: 'ok',
  escalated: 'warn',

  // outcomes
  applied: 'ok',
  succeeded: 'ok',
  passed: 'ok',
  failed: 'bad',

  // AWS SSM command states (chaos scenario runs)
  pending: 'neutral',
  delayed: 'warn',
  inprogress: 'info',
  success: 'ok',
  cancelled: 'neutral',
  cancelling: 'neutral',
  timedout: 'bad',
}

const LABELS = {
  auto_resolved: 'Auto-resolved',
  inprogress: 'In progress',
  timedout: 'Timed out',
  true: 'Healthy',
  false: 'Unhealthy',
  null: 'Unknown',
  operational: 'Operational',
  monitoring: 'Monitoring',
}

// States that mean "Sentinel (or a command) is working on this right now".
// Only these get the subtle pulsing dot — motion always reflects real state.
const LIVE = new Set(['investigating', 'remediating', 'validating', 'inprogress'])

export function statusKey(status) {
  return String(status).replace(/[\s-]/g, '').toLowerCase().replace('autoresolved', 'auto_resolved')
}

export function statusTone(status) {
  return TONE_BY_STATUS[statusKey(status)] ?? 'neutral'
}

export function statusLabel(status) {
  const key = statusKey(status)
  return LABELS[key] ?? titleCase(key)
}

export function isLiveStatus(status) {
  return LIVE.has(statusKey(status))
}

// A resolved (or auto_resolved) STATUS is authoritative and wins outright:
// the backend can leave a stale `escalated: true` on an incident it later
// resolved by another path (e.g. Alertmanager reporting the alert cleared
// while the incident was sitting escalated) without also clearing that
// flag — see the v1.3 Escalation Audit. Otherwise, the list views have
// always shown "Escalated" whenever the escalated flag is set, regardless
// of the underlying status field; keep that rule for the non-resolved case.
const RESOLVED_STATUSES = new Set(['resolved', 'auto_resolved'])

export function effectiveIncidentStatus(incident) {
  if (RESOLVED_STATUSES.has(incident.status)) return incident.status
  return incident.escalated ? 'escalated' : incident.status
}

export const HEALTH_HEADLINE = {
  operational: 'All systems operational',
  degraded: 'Degraded',
  critical: 'Critical',
  unknown: 'Status unknown',
}
