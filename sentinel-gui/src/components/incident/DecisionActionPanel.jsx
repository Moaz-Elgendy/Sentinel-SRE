import { formatDuration, formatPercent, formatTimestamp, titleCase } from '../../utils/format.js'

function ParamsSummary({ params }) {
  if (!params) return null
  const parts = []
  if (params.deployment) parts.push(`deployment: ${params.deployment}`)
  if (params.service) parts.push(`service: ${params.service}`)
  if (params.namespace) parts.push(`namespace: ${params.namespace}`)
  if (params.replicas != null) parts.push(`replicas: ${params.replicas}`)
  if (params.target_revision != null) parts.push(`target revision: ${params.target_revision}`)
  if (parts.length === 0) return null
  return <div className="mono muted">{parts.join(' · ')}</div>
}

/**
 * One `AttemptRecord` (plan -> verdict -> result -> validation), see
 * app/models/incident.py. Multiple attempts on one incident means a
 * re-investigation cycle ran (the validation of an earlier attempt failed
 * and Sentinel tried again) — each renders as its own card, oldest first.
 */
function AttemptCard({ attempt, index }) {
  const { plan, verdict, result, validation } = attempt

  return (
    <div className="attempt-card">
      <div className="attempt-card__header">
        <span className="tag">Attempt {index + 1}</span>
        <span className="muted">{formatTimestamp(attempt.at)}</span>
      </div>

      <div className="attempt-card__section">
        <div className="attempt-card__section-title">Candidate action</div>
        <div>
          {titleCase(plan.action)} · confidence {formatPercent(plan.confidence)}
        </div>
        <ParamsSummary params={plan.params} />
        {plan.rationale && <p className="muted">{plan.rationale}</p>}
      </div>

      {verdict && (
        <div className="attempt-card__section">
          <div className="attempt-card__section-title">Policy evaluation</div>
          <div className={verdict.allowed ? 'decision-tag decision-tag--allowed' : 'decision-tag decision-tag--denied'}>
            {verdict.allowed ? 'Approved' : `Denied — ${titleCase(verdict.reason)}`}
          </div>
          {verdict.detail && <p className="muted">{verdict.detail}</p>}
          {verdict.checks && Object.keys(verdict.checks).length > 0 && (
            <ul className="checks-list">
              {Object.entries(verdict.checks).map(([check, passed]) => (
                <li key={check} className={passed ? 'checks-list__pass' : 'checks-list__fail'}>
                  {passed ? '✓' : '✗'} {titleCase(check)}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {result && (
        <div className="attempt-card__section">
          <div className="attempt-card__section-title">Action taken</div>
          <div>
            {titleCase(result.action)} — {result.succeeded ? 'succeeded' : 'failed'}
            {result.dry_run && <span className="tag tag--muted"> dry run</span>}
            {' · '}
            {formatDuration(result.duration_seconds)}
          </div>
          {result.detail && <p className="muted">{result.detail}</p>}
        </div>
      )}

      {validation && (
        <div className="attempt-card__section">
          <div className="attempt-card__section-title">Recovery validation</div>
          <div
            className={
              validation.outcome === 'passed'
                ? 'decision-tag decision-tag--allowed'
                : 'decision-tag decision-tag--denied'
            }
          >
            {titleCase(validation.outcome)}
          </div>
          {validation.failed_checks?.length > 0 && (
            <p className="muted">Failed checks: {validation.failed_checks.join(', ')}</p>
          )}
          {validation.detail && <p className="muted">{validation.detail}</p>}
        </div>
      )}
    </div>
  )
}

export default function DecisionActionPanel({ attempts }) {
  if (!attempts || attempts.length === 0) {
    return <p className="muted">No remediation attempted yet.</p>
  }
  return (
    <div className="attempt-list">
      {attempts.map((attempt, index) => (
        // eslint-disable-next-line react/no-array-index-key
        <AttemptCard key={index} attempt={attempt} index={index} />
      ))}
    </div>
  )
}
