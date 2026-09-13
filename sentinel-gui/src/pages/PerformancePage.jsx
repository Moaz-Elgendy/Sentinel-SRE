import AlertBanner from '../components/AlertBanner.jsx'
import Spinner from '../components/Spinner.jsx'
import { getPerformanceSummary } from '../api/performance.js'
import { extractErrorMessage } from '../api/client.js'
import { usePolling } from '../hooks/usePolling.js'
import { formatDuration, formatPercent } from '../utils/format.js'

function StatCard({ label, value, unavailableReason }) {
  return (
    <div className="stat-card">
      <div className="stat-card__value">{unavailableReason ? '—' : value}</div>
      <div className="stat-card__label">{label}</div>
      {unavailableReason && <div className="stat-card__note muted small">{unavailableReason}</div>}
    </div>
  )
}

export default function PerformancePage() {
  const { data, error, loading } = usePolling(getPerformanceSummary, { intervalMs: 15000 })

  if (loading) return <Spinner label="Loading performance stats…" />
  if (error) return <AlertBanner>{extractErrorMessage(error, 'Could not load performance stats.')}</AlertBanner>
  if (!data) return null

  return (
    <div className="page">
      <div className="page__header">
        <h1>Sentinel Performance</h1>
      </div>
      <p className="muted">
        Computed from {data.sample_size.total_incidents} recorded incidents and {data.sample_size.executed_attempts}{' '}
        executed remediation attempts. Metrics with no data yet are shown as "—", never a guessed number.
      </p>

      <div className="grid grid--3">
        <StatCard
          label="Diagnosis accuracy"
          value={formatPercent(data.diagnosis_accuracy)}
          unavailableReason={data.diagnosis_feedback_unavailable_reason}
        />
        <StatCard label="Remediation success rate" value={formatPercent(data.remediation_success_rate)} />
        <StatCard label="Autonomous resolutions" value={data.autonomous_resolutions} />
        <StatCard label="Escalations" value={data.escalations} />
        <StatCard
          label="Temporary overrides"
          value={data.temporary_overrides}
          unavailableReason={data.temporary_overrides_unavailable_reason}
        />
        <StatCard label="Avg. incident resolution time" value={formatDuration(data.avg_incident_resolution_seconds)} />
        <StatCard
          label="Avg. time to detection"
          value={formatDuration(data.avg_time_to_detection_seconds)}
          unavailableReason={data.avg_time_to_detection_unavailable_reason}
        />
        <StatCard label="Avg. time to remediation" value={formatDuration(data.avg_time_to_remediation_seconds)} />
      </div>
    </div>
  )
}
