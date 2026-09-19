import { extractErrorMessage } from '../api/client.js'
import { getPerformanceSummary } from '../api/performance.js'
import AlertBanner from '../components/ui/AlertBanner.jsx'
import { ErrorState } from '../components/ui/EmptyState.jsx'
import LastUpdated from '../components/ui/LastUpdated.jsx'
import { PageSkeleton } from '../components/ui/Loading.jsx'
import PageHeader from '../components/ui/PageHeader.jsx'
import { usePageTitle } from '../hooks/usePageTitle.js'
import { usePolling } from '../hooks/usePolling.js'
import { formatDuration, formatPercent } from '../utils/format.js'

// A metric with no data is shown as "—" together with the reason the backend
// gives — never a guessed number.
function Metric({ label, value, unavailableReason }) {
  return (
    <div className="metric">
      <div className={`metric__value${unavailableReason ? ' metric__value--empty' : ''}`}>{unavailableReason ? '—' : value}</div>
      <div className="metric__label">{label}</div>
      {unavailableReason && <div className="metric__note">{unavailableReason}</div>}
    </div>
  )
}

function MetricGroup({ title, description, children }) {
  return (
    <section className="card" aria-label={title}>
      <div className="card__header">
        <div>
          <h2 className="card__title">{title}</h2>
          <p className="card__description">{description}</p>
        </div>
      </div>
      <div className="metric-grid">{children}</div>
    </section>
  )
}

export default function PerformancePage() {
  usePageTitle('Performance')
  const { data, error, loading, busy, updatedAt, refetch } = usePolling(getPerformanceSummary, { intervalMs: 15000 })

  if (loading && !data) return <PageSkeleton label="Loading performance stats…" cards={2} />
  if (!data) {
    return (
      <ErrorState
        title="Couldn't load performance stats"
        message={extractErrorMessage(error, 'Sentinel did not respond.')}
        onRetry={refetch}
      />
    )
  }

  return (
    <div className="page">
      <PageHeader
        title="Sentinel performance"
        subtitle={`Computed from ${data.sample_size.total_incidents} recorded incidents and ${data.sample_size.executed_attempts} executed remediation attempts. Metrics with no data yet show “—”, never a guessed number.`}
        actions={<LastUpdated updatedAt={updatedAt} stale={Boolean(error)} busy={busy} />}
      />

      {error && (
        <AlertBanner tone="warn" title="Showing the last data received">
          {extractErrorMessage(error, 'Could not refresh performance stats.')} Retrying automatically.
        </AlertBanner>
      )}

      <MetricGroup title="Outcomes" description="How often Sentinel is right, and how often it can act on its own.">
        <Metric
          label="Diagnosis accuracy"
          value={formatPercent(data.diagnosis_accuracy)}
          unavailableReason={data.diagnosis_feedback_unavailable_reason}
        />
        <Metric label="Remediation success rate" value={formatPercent(data.remediation_success_rate)} />
        <Metric label="Autonomous resolutions" value={data.autonomous_resolutions} />
        <Metric label="Escalations" value={data.escalations} />
        <Metric
          label="Temporary overrides"
          value={data.temporary_overrides}
          unavailableReason={data.temporary_overrides_unavailable_reason}
        />
      </MetricGroup>

      <MetricGroup title="Speed" description="How quickly incidents move from detection to recovery.">
        <Metric
          label="Avg. time to detection"
          value={formatDuration(data.avg_time_to_detection_seconds)}
          unavailableReason={data.avg_time_to_detection_unavailable_reason}
        />
        <Metric label="Avg. time to remediation" value={formatDuration(data.avg_time_to_remediation_seconds)} />
        <Metric label="Avg. incident resolution time" value={formatDuration(data.avg_incident_resolution_seconds)} />
      </MetricGroup>
    </div>
  )
}
