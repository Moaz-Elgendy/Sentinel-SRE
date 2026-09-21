import { lazy, Suspense } from 'react'
import { BarChart3, Timer } from 'lucide-react'
import { extractErrorMessage } from '@/api/client'
import { Panel } from '@/components/sentinel/Panel'
import { PageHeader } from '@/components/sentinel/PageHeader'
import { ErrorState, SkeletonRows } from '@/components/sentinel/States'
import { usePageTitle } from '@/hooks/usePageTitle'
import { usePerformance } from '@/hooks/usePerformance'
import { cn } from '@/lib/utils'
import { formatPercent, formatSpan } from '@/utils/format'

const IncidentActivityChart = lazy(() => import('@/components/charts/IncidentActivityChart'))
const ResolutionTimeChart = lazy(() => import('@/components/charts/ResolutionTimeChart'))

// A metric with no data shows "—" together with the reason the backend gives, never a guessed number.
function Metric({ label, value, unavailableReason, hint }) {
  return (
    <div className="px-4 py-3.5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className={cn('tnum mt-1 text-2xl leading-8 font-semibold tracking-tight', unavailableReason && 'text-muted-foreground/60')}>{unavailableReason ? '—' : value}</dd>
      {unavailableReason ? <p className="mt-1 text-xs text-muted-foreground">{unavailableReason}</p> : hint ? <p className="mt-1 text-xs text-muted-foreground">{hint}</p> : null}
    </div>
  )
}

function MetricGroup({ title, description, children, cols = 'xl:grid-cols-5' }) {
  return (
    <Panel title={title} description={description} flush>
      <dl className={`grid divide-y sm:grid-cols-2 sm:divide-x sm:divide-y-0 lg:grid-cols-3 ${cols}`}>{children}</dl>
    </Panel>
  )
}

export default function PerformancePage() {
  usePageTitle('Performance')
  const { data, error, loading, refetch } = usePerformance(15000)

  if (loading && !data) return <SkeletonRows rows={6} />
  if (!data) return <ErrorState title="Couldn’t load performance stats" message={extractErrorMessage(error, 'Sentinel did not respond.')} onRetry={refetch} className="py-24" />

  const handled = data.autonomous_resolutions + data.escalations
  const autonomousShare = handled > 0 ? data.autonomous_resolutions / handled : null

  return (
    <div className="space-y-4">
      <PageHeader
        title="Performance"
        description={`How well Sentinel is doing, computed from ${data.sample_size.total_incidents} recorded incidents and ${data.sample_size.executed_attempts} executed remediation attempts. A metric with no data shows why, never a guess.`}
      />

      <section aria-label="Autonomy" className="rounded-lg border bg-card px-4 py-3.5">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-sm font-semibold">Autonomy</h2>
          <p className="tnum text-xs text-muted-foreground">
            {data.autonomous_resolutions} resolved by Sentinel alone, {data.escalations} needed a human
          </p>
        </div>
        {autonomousShare != null ? (
          <>
            <div role="img" aria-label={`${formatPercent(autonomousShare)} of handled incidents were resolved autonomously`} className="mt-3 flex h-2.5 overflow-hidden rounded-full bg-muted">
              <div className="bg-ok-solid" style={{ width: `${autonomousShare * 100}%` }} />
              <div className="bg-warn-solid" style={{ width: `${(1 - autonomousShare) * 100}%` }} />
            </div>
            <div className="mt-2 flex justify-between text-xs">
              <span className="text-ok">Autonomous {formatPercent(autonomousShare)}</span>
              <span className="text-warn">Escalated {formatPercent(1 - autonomousShare)}</span>
            </div>
          </>
        ) : (
          <p className="mt-2 text-sm text-muted-foreground">No incidents have been closed yet.</p>
        )}
      </section>

      <MetricGroup title="Outcomes" description="How often Sentinel is right, and how often it can act on its own">
        <Metric label="Diagnosis accuracy" value={formatPercent(data.diagnosis_accuracy)} unavailableReason={data.diagnosis_feedback_unavailable_reason} />
        <Metric label="Remediation success rate" value={formatPercent(data.remediation_success_rate)} hint="Executed actions whose recovery validation passed" />
        <Metric label="Autonomous resolutions" value={data.autonomous_resolutions} />
        <Metric label="Escalations" value={data.escalations} />
        <Metric label="Temporary overrides" value={data.temporary_overrides} unavailableReason={data.temporary_overrides_unavailable_reason} />
      </MetricGroup>

      <MetricGroup title="Speed" description="How quickly incidents move from detection to recovery" cols="xl:grid-cols-3">
        <Metric label="Avg. time to detection" value={formatSpan(data.avg_time_to_detection_seconds)} unavailableReason={data.avg_time_to_detection_unavailable_reason} />
        <Metric label="Avg. time to remediation" value={formatSpan(data.avg_time_to_remediation_seconds)} hint="Alert to action executed" />
        <Metric label="Avg. resolution time" value={formatSpan(data.avg_incident_resolution_seconds)} hint="Alert to confirmed recovery" />
      </MetricGroup>

      <div className="grid gap-4 xl:grid-cols-2">
        <Panel title="Incidents per day" icon={BarChart3} description="Last 30 days, by outcome">
          <Suspense fallback={<SkeletonRows rows={3} />}>
            <IncidentActivityChart days={30} height={220} />
          </Suspense>
        </Panel>
        <Panel title="Time to resolve" icon={Timer} description="Recent resolved incidents, against the all-time average">
          <Suspense fallback={<SkeletonRows rows={3} />}>
            <ResolutionTimeChart average={data.avg_incident_resolution_seconds} />
          </Suspense>
        </Panel>
      </div>
    </div>
  )
}
