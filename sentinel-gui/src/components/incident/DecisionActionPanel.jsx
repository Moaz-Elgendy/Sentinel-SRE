import { formatDuration, formatPercent, formatTimestamp, titleCase } from '../../utils/format.js'
import Tag from '../ui/Tag.jsx'

function ParamsSummary({ params }) {
  if (!params) return null
  const parts = []
  if (params.deployment) parts.push(`deployment: ${params.deployment}`)
  if (params.service) parts.push(`service: ${params.service}`)
  if (params.namespace) parts.push(`namespace: ${params.namespace}`)
  if (params.replicas != null) parts.push(`replicas: ${params.replicas}`)
  if (params.target_revision != null) parts.push(`target revision: ${params.target_revision}`)
  if (parts.length === 0) return null
  return <div className="mono muted small">{parts.join(' · ')}</div>
}

function Step({ title, children }) {
  return (
    <div className="step">
      <div className="step__title">{title}</div>
      <div className="step__content">{children}</div>
    </div>
  )
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
    <div className="attempt">
      <div className="attempt__header">
        <Tag tone="info">Attempt {index + 1}</Tag>
        <span className="muted small">{formatTimestamp(attempt.at)}</span>
      </div>

      <Step title="Candidate action">
        <div>
          <strong>{titleCase(plan.action)}</strong> <span className="muted">· confidence {formatPercent(plan.confidence)}</span>
        </div>
        <ParamsSummary params={plan.params} />
        {plan.rationale && <p className="muted">{plan.rationale}</p>}
      </Step>

      {verdict && (
        <Step title="Policy evaluation">
          <Tag tone={verdict.allowed ? 'ok' : 'bad'}>
            {verdict.allowed ? 'Approved' : `Denied — ${titleCase(verdict.reason)}`}
          </Tag>
          {verdict.detail && <p className="muted">{verdict.detail}</p>}
          {verdict.checks && Object.keys(verdict.checks).length > 0 && (
            <ul className="checks">
              {Object.entries(verdict.checks).map(([check, passed]) => (
                <li key={check} className={passed ? 'checks__pass' : 'checks__fail'}>
                  <span aria-hidden="true">{passed ? '✓' : '✗'}</span>
                  <span className="sr-only">{passed ? 'Passed: ' : 'Failed: '}</span>
                  {titleCase(check)}
                </li>
              ))}
            </ul>
          )}
        </Step>
      )}

      {result && (
        <Step title="Action taken">
          <div className="cluster">
            <strong>{titleCase(result.action)}</strong>
            <Tag tone={result.succeeded ? 'ok' : 'bad'}>{result.succeeded ? 'Succeeded' : 'Failed'}</Tag>
            {result.dry_run && <Tag title="Decided and authorised normally, but not applied to the cluster">Dry run</Tag>}
            <span className="muted small num">{formatDuration(result.duration_seconds)}</span>
          </div>
          {result.detail && <p className="muted">{result.detail}</p>}
        </Step>
      )}

      {validation && (
        <Step title="Recovery validation">
          <Tag tone={validation.outcome === 'passed' ? 'ok' : 'bad'}>{titleCase(validation.outcome)}</Tag>
          {validation.failed_checks?.length > 0 && (
            <p className="muted">Failed checks: {validation.failed_checks.join(', ')}</p>
          )}
          {validation.detail && <p className="muted">{validation.detail}</p>}
        </Step>
      )}
    </div>
  )
}

export default function DecisionActionPanel({ attempts }) {
  if (!attempts || attempts.length === 0) {
    return <p className="muted">No remediation attempted yet.</p>
  }
  return (
    <div className="stack">
      {attempts.map((attempt, index) => (
        // eslint-disable-next-line react/no-array-index-key
        <AttemptCard key={index} attempt={attempt} index={index} />
      ))}
    </div>
  )
}
