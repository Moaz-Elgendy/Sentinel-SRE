import { formatDuration, formatTimestamp } from '../../utils/format.js'

const TERMINAL_STATUSES = new Set(['resolved', 'escalated', 'auto_resolved'])

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

    const nextPhaseAt = order.slice(index + 1).map((p) => firstSeenAt[p]).find((v) => v != null)
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

  return (
    <div className="flow-diagram">
      <ol className="flow-diagram__list">
        {stages.map((stage) => (
          <li key={stage.phase} className={`flow-stage flow-stage--${stage.status}`}>
            <div className="flow-stage__marker" aria-hidden="true">
              {stage.status === 'done' && '✓'}
              {stage.status === 'active' && '●'}
              {stage.status === 'pending' && '○'}
            </div>
            <div className="flow-stage__body">
              <div className="flow-stage__label">{stage.label}</div>
              {stage.at != null && (
                <div className="flow-stage__meta">
                  {formatTimestamp(stage.at)}
                  {stage.duration != null && <> · took {formatDuration(stage.duration)}</>}
                </div>
              )}
              {stage.message && <div className="flow-stage__message">{stage.message}</div>}
            </div>
          </li>
        ))}
        {resolvedNode && (
          <li className="flow-stage flow-stage--done flow-stage--terminal">
            <div className="flow-stage__marker" aria-hidden="true">✓</div>
            <div className="flow-stage__body">
              <div className="flow-stage__label">{resolvedNode.label}</div>
              {resolvedNode.at != null && (
                <div className="flow-stage__meta">{formatTimestamp(resolvedNode.at)}</div>
              )}
            </div>
          </li>
        )}
      </ol>

      {incident.escalated && (
        <div className="flow-branch flow-branch--escalated">
          <div className="flow-branch__connector" aria-hidden="true" />
          <div className="flow-branch__body">
            <div className="flow-branch__label">⚠ Escalated to SRE</div>
            <div className="flow-branch__detail">{incident.escalation_detail || 'No safe autonomous action available.'}</div>
          </div>
        </div>
      )}
    </div>
  )
}
