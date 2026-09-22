import { lazy, Suspense } from 'react'
import { BarChart3, FlaskConical, Timer } from 'lucide-react'
import { extractErrorMessage } from '@/api/client'
import { Panel } from '@/components/sentinel/Panel'
import { PageHeader } from '@/components/sentinel/PageHeader'
import { EmptyState, ErrorState, SkeletonRows } from '@/components/sentinel/States'
import { StatusBadge } from '@/components/sentinel/StatusBadge'
import { Timestamp } from '@/components/sentinel/Timestamp'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { usePageTitle } from '@/hooks/usePageTitle'
import { usePerformance } from '@/hooks/usePerformance'
import { useEvaluation } from '@/hooks/useEvaluation'
import { cn } from '@/lib/utils'
import { formatPercent, formatSpan, sentenceCase } from '@/utils/format'
import { actionLabel, rootCauseLabel } from '@/utils/incident'

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

// "db-outage" -> "Db outage". Chaos scenario ids are hyphenated, unlike the
// underscored root-cause/action enums sentenceCase already handles.
function scenarioLabel(scenario) {
  return sentenceCase(scenario?.replace(/-/g, '_'))
}

// True/False/null through StatusBadge's existing tone table (ok/bad/neutral)
// — same trick as boolean health checks elsewhere in the console — with
// wording specific to a ground-truth comparison rather than "Healthy/Unhealthy".
function VerdictBadge({ value, whenTrue, whenFalse }) {
  const label = value == null ? 'No ground truth' : value ? whenTrue : whenFalse
  return <StatusBadge status={value} label={label} />
}

function EvaluationRunsTable({ runs }) {
  if (runs.length === 0) {
    return (
      <EmptyState
        icon={FlaskConical}
        compact
        title="No evaluation runs recorded yet"
        description="Record one from a chaos scenario (POST /api/evaluation/runs) to start tracking Sentinel's decisions against a known outcome."
      />
    )
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Scenario</TableHead>
          <TableHead>App</TableHead>
          <TableHead>Triggered</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>Root cause</TableHead>
          <TableHead>Decision</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {runs.map((run) => (
          <TableRow key={run.id}>
            <TableCell className="font-medium">{scenarioLabel(run.scenario)}</TableCell>
            <TableCell className="text-muted-foreground">{run.app}</TableCell>
            <TableCell>
              <Timestamp value={run.triggered_at} />
            </TableCell>
            <TableCell>
              {!run.resolved ? (
                <StatusBadge status="pending" label="Watching for an incident" />
              ) : run.incident_id ? (
                <StatusBadge status={run.final_status} label={sentenceCase(run.final_status)} />
              ) : (
                <StatusBadge status="unknown" label="No incident appeared" />
              )}
            </TableCell>
            <TableCell>
              {run.incident_id ? (
                <div className="flex flex-col gap-1">
                  <VerdictBadge value={run.root_cause_correct} whenTrue="Correct" whenFalse="Incorrect" />
                  {run.root_cause_correct === false && (
                    <span className="text-xs text-muted-foreground">got {rootCauseLabel(run.actual_root_cause)}</span>
                  )}
                </div>
              ) : (
                <span className="text-muted-foreground">—</span>
              )}
            </TableCell>
            <TableCell>
              {run.incident_id ? (
                <div className="flex flex-col gap-1">
                  <VerdictBadge value={run.decision_matched_expected} whenTrue="As expected" whenFalse="Diverged" />
                  {run.decision_matched_expected === false && (
                    <span className="text-xs text-muted-foreground">did {actionLabel(run.actual_first_action)}</span>
                  )}
                </div>
              ) : (
                <span className="text-muted-foreground">—</span>
              )}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

export default function PerformancePage() {
  usePageTitle('Performance')
  const { data, error, loading, refetch } = usePerformance(15000)
  const { data: evaluation } = useEvaluation(20000)

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

      <MetricGroup
        title="Decision quality"
        description="Failure modes remediation success rate alone conflates, plus rates nothing else surfaces"
        cols="xl:grid-cols-4"
      >
        <Metric
          label="First-action success rate"
          value={formatPercent(data.first_action_success_rate)}
          unavailableReason={data.first_action_success_rate_unavailable_reason}
          hint="Only Sentinel's first executed attempt per incident, before any fallback"
        />
        <Metric
          label="Execution failure rate"
          value={formatPercent(data.execution_failure_rate)}
          unavailableReason={data.execution_failure_rate_unavailable_reason}
          hint="Executed attempts that failed to apply at all"
        />
        <Metric
          label="Ineffective remediation rate"
          value={formatPercent(data.ineffective_remediation_rate)}
          unavailableReason={data.ineffective_remediation_rate_unavailable_reason}
          hint="Applied fine, but validation still failed"
        />
        <Metric
          label="Recovery validation success rate"
          value={formatPercent(data.recovery_validation_success_rate)}
          unavailableReason={data.recovery_validation_success_rate_unavailable_reason}
          hint="Of every attempt with a validation outcome recorded"
        />
        <Metric
          label="Policy rejection rate"
          value={formatPercent(data.policy_rejection_rate)}
          unavailableReason={data.policy_rejection_rate_unavailable_reason}
          hint="Of every candidate action the Policy Engine ruled on"
        />
        <Metric
          label="Escalation rate"
          value={formatPercent(data.escalation_rate)}
          unavailableReason={data.escalation_rate_unavailable_reason}
          hint="Share of all recorded incidents"
        />
      </MetricGroup>

      <MetricGroup title="Speed" description="How quickly incidents move from detection to recovery" cols="xl:grid-cols-4">
        <Metric label="Avg. time to detection" value={formatSpan(data.avg_time_to_detection_seconds)} unavailableReason={data.avg_time_to_detection_unavailable_reason} />
        <Metric label="Avg. investigation latency" value={formatSpan(data.avg_investigation_latency_seconds)} hint="Detection to root-cause diagnosis" />
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

      <Panel
        title="Chaos-scenario evaluation"
        icon={FlaskConical}
        description="Decision quality against a known right answer — only available where a chaos scenario carries an unambiguous expected outcome"
        flush
      >
        {evaluation ? (
          <>
            <dl className="grid divide-y border-b sm:grid-cols-2 sm:divide-x sm:divide-y-0 lg:grid-cols-4">
              <Metric
                label="RCA correctness rate"
                value={formatPercent(evaluation.summary.rca_correctness_rate)}
                hint="Root cause matched the scenario's known cause"
              />
              <Metric
                label="Decision accuracy rate"
                value={formatPercent(evaluation.summary.decision_accuracy_rate)}
                hint="First action matched the scenario's expected action"
              />
              <Metric label="Concluded runs" value={evaluation.summary.sample_size.concluded_runs} hint={`of ${evaluation.summary.sample_size.total_runs} recorded`} />
              <Metric label="Pending runs" value={evaluation.summary.sample_size.pending_runs} hint="Still watching for a matching incident" />
            </dl>
            <div className="px-4 py-3">
              <EvaluationRunsTable runs={evaluation.runs} />
            </div>
          </>
        ) : (
          <SkeletonRows rows={3} />
        )}
      </Panel>
    </div>
  )
}
