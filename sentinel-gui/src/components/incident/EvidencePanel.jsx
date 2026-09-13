import { formatTimestamp } from '../../utils/format.js'

function Fact({ label, value }) {
  if (value == null || value === '') return null
  return (
    <div className="fact-row">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  )
}

/**
 * Renders `Incident.evidence` (see app/models/incident.py: Evidence) as
 * plain facts — never mixed with Sentinel's interpretation of them. That
 * distinction (facts vs. reasoning vs. decisions) is an explicit GUI spec
 * requirement; ReasoningPanel is the only place a "why" appears.
 */
export default function EvidencePanel({ evidence }) {
  if (!evidence) {
    return <p className="muted">No evidence collected yet.</p>
  }

  const deploy = evidence.deploy_commit

  return (
    <div className="evidence-panel">
      <dl className="fact-list">
        <Fact
          label="HTTP error rate"
          value={evidence.error_rate != null ? `${(evidence.error_rate * 100).toFixed(1)}%` : null}
        />
        <Fact label="Request rate" value={evidence.request_rate != null ? `${evidence.request_rate.toFixed(2)}/s` : null} />
        <Fact label="CPU" value={evidence.cpu_cores != null ? `${evidence.cpu_cores.toFixed(2)} cores` : null} />
        <Fact
          label="Memory"
          value={evidence.memory_bytes != null ? `${(evidence.memory_bytes / 1e6).toFixed(0)} MB` : null}
        />
        <Fact
          label="Memory growth (30m)"
          value={
            evidence.memory_growth_bytes != null
              ? `${(evidence.memory_growth_bytes / 1e6).toFixed(0)} MB`
              : null
          }
        />
        <Fact label="Pod up" value={evidence.up != null ? (evidence.up === 1 ? 'yes' : 'no') : null} />
        <Fact label="Health status" value={evidence.health_status} />
        <Fact label="Health check code" value={evidence.health_http_code} />
        <Fact label="Restart count" value={evidence.restart_count_total} />
        <Fact
          label="Latest revision age"
          value={
            evidence.latest_revision_age_seconds != null
              ? `${Math.round(evidence.latest_revision_age_seconds / 60)} min`
              : null
          }
        />
        <Fact label="Log errors" value={evidence.log_error_count} />
        {deploy && (
          <Fact
            label="Deployment"
            value={`${deploy.sha ? deploy.sha.slice(0, 8) : ''} ${deploy.message ?? ''}`.trim()}
          />
        )}
        <Fact label="Collected at" value={formatTimestamp(evidence.collected_at)} />
      </dl>

      {evidence.correlations?.length > 0 && (
        <div className="evidence-sublist">
          <h3>Correlated signals</h3>
          <ul>
            {evidence.correlations.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      )}

      {evidence.log_sample_messages?.length > 0 && (
        <div className="evidence-sublist">
          <h3>Sample log lines</h3>
          <ul className="mono-list">
            {evidence.log_sample_messages.slice(0, 5).map((line, i) => (
              // eslint-disable-next-line react/no-array-index-key
              <li key={i}>{line}</li>
            ))}
          </ul>
        </div>
      )}

      {evidence.errors?.length > 0 && (
        <div className="evidence-sublist evidence-sublist--warn">
          <h3>Collection errors</h3>
          <ul>
            {evidence.errors.map((err) => (
              <li key={err}>{err}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
