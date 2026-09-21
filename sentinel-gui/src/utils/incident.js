import { effectiveIncidentStatus } from './status.js'

/**
 * Pure derivations over the real incident record (sentinel-ai's Incident.to_dict).
 * Nothing here invents data: every value is read from, or computed from, fields
 * the API already returns.
 */

export const TERMINAL_STATUSES = new Set(['resolved', 'escalated', 'auto_resolved'])

export const isActiveIncident = (incident) => !TERMINAL_STATUSES.has(incident.status)
export const isResolvedIncident = (incident) => incident.status === 'resolved' || incident.status === 'auto_resolved'
/** `escalated` is cleared by the backend when an authorized action starts executing. */
export const isAwaitingHuman = (incident) => incident.escalated === true || incident.status === 'escalated'
export const incidentStatus = (incident) => effectiveIncidentStatus(incident)

/** One human sentence for what is wrong; the alert's own summary when it has one. */
export function incidentHeadline(incident) {
  return incident.summary || incident.description || `${incident.alertname} on ${incident.app ?? 'unknown service'}`
}

export const lastEvent = (incident) => incident.timeline?.[incident.timeline.length - 1] ?? null
export const lastAttempt = (incident) => incident.attempts?.[incident.attempts.length - 1] ?? null
export const executedAttempts = (incident) => (incident.attempts ?? []).filter((a) => a.result)

/** The action Sentinel most recently carried out (not merely considered). */
export function lastExecuted(incident) {
  const executed = executedAttempts(incident)
  return executed[executed.length - 1] ?? null
}

export const isHumanAuthorized = (attempt) => attempt?.verdict?.checks?.confidence_human_override === true

export function endedAt(incident) {
  if (incident.resolved_at) return incident.resolved_at
  return TERMINAL_STATUSES.has(incident.status) ? incident.updated_at : null
}

/** Seconds from alert to close (or to now, while still open). */
/** "Elapsed" while open, "Took" once closed, and for an escalation the time Sentinel needed to reach it. */
export function durationLabel(incident) {
  if (isActiveIncident(incident)) return 'Elapsed'
  return incident.escalated ? 'Time to escalate' : 'Duration'
}

export function incidentDuration(incident, nowSeconds = Date.now() / 1000) {
  const end = endedAt(incident) ?? nowSeconds
  return Math.max(0, end - incident.created_at)
}

export function isValidationPassed(attempt) {
  return attempt?.validation?.outcome === 'passed'
}

// ---------------------------------------------------------------------------
// Lifecycle stages
// ---------------------------------------------------------------------------

// Short, action-oriented names for the rail. The authoritative phase ids and
// order come from the backend (GET /api/meta/lifecycle-phases); only the
// presentation wording lives here.
export const STAGE_SHORT_LABEL = {
  detection: 'Detect',
  investigation: 'Evidence',
  correlation: 'Correlate',
  root_cause_analysis: 'Diagnose',
  remediation_decision: 'Decide',
  policy_check: 'Policy',
  autonomous_execution: 'Act',
  recovery_validation: 'Validate',
  documentation: 'Document',
}

// Phases outside the primary flow are folded into the stage they belong to.
const PHASE_TO_STAGE = {
  re_investigation: 'investigation',
  notification: 'documentation',
  learning: 'documentation',
}

/**
 * Stage states:
 *   done      reached and passed
 *   active    Sentinel is working on it right now
 *   pending   not reached yet (incident still open)
 *   skipped   never reached (incident ended before this stage)
 *   blocked   policy refused the action, which ended the automated path
 *   failed    the action or its validation did not succeed
 */
export function lifecycleStages(incident, meta, nowSeconds = Date.now() / 1000) {
  const order = meta?.primary_flow_order?.length ? meta.primary_flow_order : Object.keys(STAGE_SHORT_LABEL)
  const timeline = incident.timeline ?? []
  const terminal = TERMINAL_STATUSES.has(incident.status)
  const closeAt = endedAt(incident) ?? nowSeconds

  // Attribute the time between consecutive events to the stage of the earlier one.
  const byStage = new Map()
  timeline.forEach((event, i) => {
    const stage = PHASE_TO_STAGE[event.phase] ?? event.phase
    const next = timeline[i + 1]
    const until = next ? next.at : closeAt
    const entry = byStage.get(stage) ?? { at: event.at, seconds: 0, message: event.message, events: 0 }
    entry.seconds += Math.max(0, until - event.at)
    entry.message = event.message
    entry.events += 1
    byStage.set(stage, entry)
  })

  const currentPhase = lastEvent(incident)?.phase
  const currentStage = PHASE_TO_STAGE[currentPhase] ?? currentPhase

  const final = lastAttempt(incident)
  const finalExecuted = lastExecuted(incident)
  const policyBlocked = terminal && incident.escalated && final?.verdict && final.verdict.allowed === false
  const executionFailed = Boolean(finalExecuted) && finalExecuted.result?.succeeded === false
  const validationFailed =
    Boolean(finalExecuted?.validation) && !isValidationPassed(finalExecuted) && Boolean(incident.escalated)

  return order.map((id) => {
    const reached = byStage.get(id)
    let state
    if (reached) {
      state = !terminal && currentStage === id ? 'active' : 'done'
    } else {
      state = terminal ? 'skipped' : 'pending'
    }
    if (id === 'policy_check' && policyBlocked && reached) state = 'blocked'
    if (id === 'autonomous_execution' && executionFailed && reached) state = 'failed'
    if (id === 'recovery_validation' && validationFailed && reached) state = 'failed'
    return {
      id,
      label: STAGE_SHORT_LABEL[id] ?? id,
      fullLabel: meta?.labels?.[id] ?? id,
      state,
      at: reached?.at ?? null,
      seconds: reached?.seconds ?? null,
      message: reached?.message ?? null,
    }
  })
}

/** The trailing node of the rail: where the incident ended up (or will). */
export function lifecycleOutcome(incident) {
  if (incident.escalated) return { id: 'escalation', label: 'Needs you', state: 'attention' }
  if (isResolvedIncident(incident)) {
    return { id: 'resolved', label: incident.status === 'auto_resolved' ? 'Cleared' : 'Recovered', state: 'ok' }
  }
  return { id: 'resolved', label: 'Recovered', state: 'pending' }
}

export function reinvestigationCount(incident) {
  return (incident.timeline ?? []).filter((e) => e.phase === 're_investigation').length
}

// ---------------------------------------------------------------------------
// Outcome narrative — the one-line answer to "what happened?"
// ---------------------------------------------------------------------------

export function incidentOutcome(incident, nowSeconds = Date.now() / 1000) {
  const executed = lastExecuted(incident)
  const duration = incidentDuration(incident, nowSeconds)
  if (incident.escalated) {
    return { kind: 'awaiting', tone: 'warn', title: 'Waiting for an SRE', detail: incident.escalation_detail ?? null }
  }
  if (incident.status === 'resolved') {
    if (executed && isValidationPassed(executed)) {
      return {
        kind: 'recovered',
        tone: 'ok',
        title: isHumanAuthorized(executed) ? 'Recovered with SRE authorization' : 'Recovered autonomously',
        detail: executed.validation?.detail ?? null,
        duration,
      }
    }
    return { kind: 'recovered', tone: 'ok', title: 'Resolved', detail: null, duration }
  }
  if (incident.status === 'auto_resolved') {
    return {
      kind: 'cleared',
      tone: 'ok',
      title: 'Cleared before remediation',
      detail: 'The alert resolved on its own, so Sentinel took no action.',
      duration,
    }
  }
  const phase = lastEvent(incident)
  return { kind: 'in_progress', tone: 'info', title: 'In progress', detail: phase?.message ?? null, duration }
}

// ---------------------------------------------------------------------------
// Wording
// ---------------------------------------------------------------------------

export const ACTION_LABEL = {
  restart_deployment: 'Restart deployment',
  rollback_deployment: 'Roll back deployment',
  scale_deployment: 'Scale deployment',
  reset_chaos_fault: 'Reset chaos fault',
  escalate: 'Escalate to an SRE',
}

export function actionLabel(action) {
  if (!action) return '—'
  return ACTION_LABEL[action] ?? String(action).replace(/_/g, ' ')
}

/** Short verb phrase for a table cell / timeline: "Restarted deployment". */
export const ACTION_PAST = {
  restart_deployment: 'Restarted deployment',
  rollback_deployment: 'Rolled back deployment',
  scale_deployment: 'Scaled deployment',
  reset_chaos_fault: 'Reset chaos fault',
}

export function rootCauseLabel(rootCause) {
  if (!rootCause) return '—'
  const text = String(rootCause)
    .replace(/_/g, ' ')
    .replace(/\b(http|cpu|db)\b/gi, (m) => m.toUpperCase())
  return text.charAt(0).toUpperCase() + text.slice(1)
}

/** The unknown root cause is Sentinel saying "I don't recognise this", not a diagnosis. */
export const isDiagnosed = (incident) => Boolean(incident.hypothesis) && incident.hypothesis.root_cause !== 'unknown'

/** What Sentinel did (or was stopped from doing) on this incident, in one cell's worth of data. */
export function actionSummary(incident) {
  const executed = lastExecuted(incident)
  if (executed) {
    const succeeded = executed.result?.succeeded !== false
    const validated = executed.validation ? isValidationPassed(executed) : null
    return {
      kind: 'executed',
      label: ACTION_PAST[executed.plan?.action] ?? actionLabel(executed.plan?.action),
      ok: succeeded && validated !== false,
      human: isHumanAuthorized(executed),
      dryRun: executed.result?.dry_run === true,
    }
  }
  const attempt = lastAttempt(incident)
  if (attempt?.verdict && attempt.verdict.allowed === false) {
    return { kind: 'blocked', label: actionLabel(attempt.plan?.action), reason: attempt.verdict.reason }
  }
  return null
}
