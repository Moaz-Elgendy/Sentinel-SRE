import { GitCommitHorizontal, SearchX, TriangleAlert } from 'lucide-react'
import { CircleAlert } from 'lucide-react'
import { Callout, EmptyState } from '@/components/sentinel/States'
import { MetricMeter } from '@/components/sentinel/Meters'
import { formatBytes, formatSpan } from '@/utils/format'
import { isActiveIncident } from '@/utils/incident'
import { CaseStep } from './CaseStep.jsx'

const pct = (v) => `${(v * 100).toFixed(v < 0.1 ? 1 : 0)}%`

function Stat({ label, value, hint }) {
  return (
    <div className="min-w-0" title={hint}>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="tnum mt-0.5 truncate text-sm font-medium">{value}</dd>
    </div>
  )
}

/** Step 1: the raw signals Sentinel collected, set against the limits it judges them by. */
export function ObservedSection({ incident, limits }) {
  const ev = incident.evidence
  const collected = Boolean(ev?.collected_at)

  if (!collected) {
    return (
      <CaseStep step="1" title="What Sentinel observed" description="Metrics, logs and cluster state gathered for this alert">
        <EmptyState compact icon={SearchX} title={isActiveIncident(incident) ? 'Evidence is still being collected' : 'No evidence was recorded'} description={isActiveIncident(incident) ? 'It appears here as soon as Sentinel has queried Prometheus, Loki and Kubernetes.' : 'Sentinel escalated or closed this incident before evidence collection completed.'} />
      </CaseStep>
    )
  }

  const meters = [
    ev.error_rate != null && { label: 'Error rate', value: ev.error_rate, display: pct(ev.error_rate), limit: limits?.max_error_rate, limitDisplay: limits && pct(limits.max_error_rate) },
    ev.p95_latency_seconds != null && { label: 'p95 latency', value: ev.p95_latency_seconds, display: `${ev.p95_latency_seconds.toFixed(2)} s`, limit: limits?.max_p95_seconds, limitDisplay: limits && `${limits.max_p95_seconds} s` },
    ev.cpu_cores != null && { label: 'CPU', value: ev.cpu_cores, display: `${ev.cpu_cores.toFixed(2)} cores`, limit: limits?.max_cpu_cores, limitDisplay: limits && `${limits.max_cpu_cores} cores` },
    ev.memory_bytes != null && { label: 'Memory', value: ev.memory_bytes, display: formatBytes(ev.memory_bytes), limit: limits?.max_memory_bytes, limitDisplay: limits && formatBytes(limits.max_memory_bytes) },
  ].filter(Boolean)

  const health = ev.health_status ? `${ev.health_status}${ev.health_http_code ? ` (HTTP ${ev.health_http_code})` : ''}` : null
  const stats = [
    ev.up != null && { label: 'Availability', value: ev.up >= 1 ? 'Up' : 'Down' },
    ev.restart_count_total != null && { label: 'Container restarts', value: ev.restart_count_total },
    ev.log_error_count != null && { label: 'Error log lines', value: ev.log_error_count },
    ev.request_rate != null && { label: 'Request rate', value: `${ev.request_rate.toFixed(1)}/s` },
    ev.memory_growth_bytes != null && { label: 'Memory growth', value: `${ev.memory_growth_bytes >= 0 ? '+' : ''}${formatBytes(ev.memory_growth_bytes)}`, hint: 'Working-set growth over the evidence window' },
    health && { label: 'Health endpoint', value: health },
    ev.latest_revision_age_seconds != null && { label: 'Newest revision age', value: formatSpan(ev.latest_revision_age_seconds) },
    ev.error_rate_5xx_count != null && { label: '5xx responses', value: ev.error_rate_5xx_count },
  ].filter(Boolean)

  const commit = ev.deploy_commit
  const noMetrics = meters.length === 0

  return (
    <CaseStep step="1" title="What Sentinel observed" description="Signals collected for this alert, against the limits it judges them by">
      <div className="space-y-4">
        {noMetrics && (
          <Callout tone="neutral" icon={CircleAlert} title="No metrics were available">
            Prometheus could not be queried, so Sentinel diagnosed from the alert and cluster state alone.
          </Callout>
        )}
        {meters.length > 0 && (
          <div className="grid gap-x-8 gap-y-4 sm:grid-cols-2">
            {meters.map((m) => (
              <MetricMeter key={m.label} {...m} />
            ))}
          </div>
        )}
        {stats.length > 0 && <dl className="grid grid-cols-2 gap-x-6 gap-y-3 border-t pt-3.5 sm:grid-cols-4">{stats.map((s) => <Stat key={s.label} {...s} />)}</dl>}

        {ev.correlations?.length > 0 && (
          <div className="border-t pt-3.5">
            <h3 className="text-xs font-medium text-muted-foreground">Signals Sentinel correlated</h3>
            <ul className="mt-1.5 space-y-1 text-sm">
              {ev.correlations.map((c) => (
                <li key={c} className="flex gap-2">
                  <span aria-hidden="true" className="mt-2 size-1 shrink-0 rounded-full bg-muted-foreground" />
                  {c}
                </li>
              ))}
            </ul>
          </div>
        )}

        {commit && (
          <div className="flex items-start gap-2.5 rounded-md border bg-muted/30 px-3 py-2 text-sm">
            <GitCommitHorizontal aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
            <div className="min-w-0">
              <p className="text-xs text-muted-foreground">Suspect deployment</p>
              <p className="break-words">
                {commit.url ? (
                  <a href={commit.url} target="_blank" rel="noopener noreferrer" className="font-mono text-xs underline underline-offset-2">
                    {String(commit.sha).slice(0, 8)}
                  </a>
                ) : (
                  <span className="font-mono text-xs">{String(commit.sha).slice(0, 8)}</span>
                )}{' '}
                {commit.message}
                {commit.author && <span className="text-muted-foreground"> by {commit.author}</span>}
              </p>
            </div>
          </div>
        )}

        {ev.errors?.length > 0 && (
          <Callout tone="warn" icon={TriangleAlert} title="Some evidence could not be collected">
            <ul className="list-inside list-disc">
              {ev.errors.map((e) => (
                <li key={e}>{e}</li>
              ))}
            </ul>
          </Callout>
        )}
      </div>
    </CaseStep>
  )
}
