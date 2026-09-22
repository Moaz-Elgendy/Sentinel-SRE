import { formatClock, formatDuration } from '../../utils/format.js'
import Icon from '../ui/Icon.jsx'

const TERMINAL_STATUSES = new Set(['resolved', 'escalated', 'auto_resolved'])

const STATE_TEXT = { done: 'Completed', active: 'In progress', pending: 'Not reached yet' }

/**
 * Renders the mandated lifecycle spine (Detection -> ... -> Documentation)
 * purely from `incident.timeline` and `incident.phase`/`status` — see the
 * approved GUI plan, section 13 ("How the live incident flow gets its state
 * from the backend"). There is no separate animation state and no
 * client-side timer faking progress: a stage is "done" because a real
 * TimelineEvent for that phase exists, "active" because it equals the
 * incident's current phase while the incident is non-terminal, and
 * "pending" otherwise. A hard page refresh mid-incident renders identically
 * to what a live update would have shown, because both read the same
 * fields.
 *
 * Presentation only: the visual pulse on the active step exists because that
 * step is genuinely in progress, and it is also stated in text for screen
 * readers ("In progress").
 */
export default function LiveFlowDiagram({ incident, phaseMeta }) {
  const order = phaseMeta?.primary_flow_order ?? []
  const labels = phaseMeta?.labels ?? {}

  // First time each phase appears in the real timeline — used for both the
  // done/active/pending determination and the per-stage timestamp/duration.
  const firstSeenAt = {}
  const detailByPhase = {}
  for (const event of incident.timeline ?? []) {
    if (!(event.phase in firstSeenAt)) {
      firstSeenAt[event.phase] = event.at
      detailByPhase[event.phase] = event.message
    }
  }

  const isTerminal = TERMINAL_STATUSES.has(incident.status)
  const stages = order.map((phase, index) => {
    const at = firstSeenAt[phase]
    const reached = at != null
    const isCurrent = incident.phase === phase && !isTerminal
    let status = 'pending'
    if (reached) status = isCurrent ? 'active' : 'done'

    const nextPhaseAt = order
      .slice(index + 1)
      .map((p) => firstSeenAt[p])
      .find((v) => v != null)
    const duration = reached && nextPhaseAt != null ? nextPhaseAt - at : null

    return {
      phase,
      label: labels[phase] ?? phase,
      status,
      at,
      duration,
      message: detailByPhase[phase],
    }
  })

  // Resolved/auto-resolved incidents get an explicit terminal node — the
  // spec's example flow ends in "System Recovered", not silently at
  // "Documentation".
  const resolvedNode =
    incident.status === 'resolved' || incident.status === 'auto_resolved'
      ? { label: 'System Recovered', at: incident.resolved_at }
      : null

  if (stages.length === 0) {
    return <p className="muted">The lifecycle phases haven't loaded yet.</p>
  }

  return (
    <div className="flow">
      <ol className="flow__list">
        {stages.map((stage) => (
          <li
            key={stage.phase}
            className={`flow-stage flow-stage--${stage.status}`}
            aria-current={stage.status === 'active' ? 'step' : undefined}
          >
            <span className="flow-stage__marker" aria-hidden="true">
              {stage.status === 'done' && <Icon name="check" size={12} />}
              {stage.status === 'active' && <span className="flow-stage__core" />}
            </span>
            <div className="flow-stage__body">
              <div className="flow-stage__label">
                {stage.label}
                <span className="sr-only"> — {STATE_TEXT[stage.status]}</span>
              </div>
              {stage.at != null && (
                <div className="flow-stage__meta">
                  {formatClock(stage.at)}
                  {stage.duration != null && <> · took {formatDuration(stage.duration)}</>}
                </div>
              )}
              {stage.message && <div className="flow-stage__message">{stage.message}</div>}
            </div>
          </li>
        ))}
        {resolvedNode && (
          <li className="flow-stage flow-stage--done flow-stage--terminal">
            <span className="flow-stage__marker" aria-hidden="true">
              <Icon name="check" size={12} />
            </span>
            <div className="flow-stage__body">
              <div className="flow-stage__label">{resolvedNode.label}</div>
              {resolvedNode.at != null && <div className="flow-stage__meta">{formatClock(resolvedNode.at)}</div>}
            </div>
          </li>
        )}
      </ol>

      {incident.status === 'escalated' && (
        <div className="flow-branch">
          <Icon name="alertTriangle" size={16} className="flow-branch__icon" />
          <div>
            <div className="flow-branch__label">Escalated to SRE</div>
            {/* While escalated, the reason is already shown in the action-required card above. */}
          </div>
        </div>
      )}

      {/* A resolved/auto_resolved incident that was escalated at some point
          before it reopened and recovered: this is history, not the current
          state, so it renders as a quiet, non-alarming note rather than the
          "Escalated to SRE" branch above (see the v1.3 Escalation Audit —
          `escalated` alone is not trustworthy once the incident is resolved,
          but `escalation_record` is still useful, real, past-tense context). */}
      {isTerminal && incident.status !== 'escalated' && incident.escalation_record?.at && (
        <div className="flow-branch flow-branch--muted">
          <Icon name="history" size={16} className="flow-branch__icon" />
          <div>
            <div className="flow-branch__label">Previously escalated</div>
            <div className="flow-branch__detail">
              {formatClock(incident.escalation_record.at)}
              {incident.escalation_record.detail ? ` — ${incident.escalation_record.detail}` : ''}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
