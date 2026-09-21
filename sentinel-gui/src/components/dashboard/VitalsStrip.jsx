import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { formatPercent, formatSpan } from '@/utils/format'
import { Timestamp } from '../sentinel/Timestamp.jsx'
import { TONE_TEXT } from '../sentinel/tone.js'

function Vital({ label, scope, value, sub, tone, hint }) {
  const body = (
    <div className="px-4 py-3">
      <dt className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
        <span>{label}</span>
        <span className="text-[11px] text-muted-foreground/70">{scope}</span>
      </dt>
      <dd className={cn('tnum mt-1 text-2xl leading-8 font-semibold tracking-tight', tone && TONE_TEXT[tone])}>{value}</dd>
      <p className="mt-0.5 min-h-4 truncate text-xs text-muted-foreground">{sub}</p>
    </div>
  )
  if (!hint) return body
  return (
    <Tooltip>
      <TooltipTrigger asChild>{body}</TooltipTrigger>
      <TooltipContent className="max-w-64">{hint}</TooltipContent>
    </Tooltip>
  )
}

/**
 * Six numbers, split honestly into "right now" and "all time" — the old
 * dashboard mixed the two and made a healthy history look like a healthy system.
 */
export function VitalsStrip({ summary, performance, active, awaiting }) {
  const services = summary?.system_health?.services ?? []
  const healthy = services.filter((s) => s.healthy === true).length
  const unhealthy = services.filter((s) => s.healthy === false).length
  const unknown = services.filter((s) => s.healthy == null).length
  const incidents = summary?.incidents
  const oldest = awaiting.length ? Math.min(...awaiting.map((i) => i.updated_at)) : null

  const serviceValue = services.length === 0 ? '—' : unknown === services.length ? 'Unknown' : `${healthy}/${services.length}`
  const serviceSub = unhealthy > 0 ? `${unhealthy} unhealthy` : unknown > 0 ? (unknown === services.length ? 'Health can’t be measured' : `${unknown} unknown`) : 'All healthy'

  return (
    <dl className="grid grid-cols-2 divide-x divide-y overflow-hidden rounded-lg border bg-card sm:grid-cols-3 xl:grid-cols-6 xl:divide-y-0">
      <Vital label="Services" scope="Now" value={serviceValue} sub={serviceSub} tone={unhealthy > 0 ? 'bad' : undefined} />
      <Vital label="In progress" scope="Now" value={active.length} sub={active.length ? 'Sentinel is on it' : 'Nothing running'} tone={active.length ? 'info' : undefined} />
      <Vital
        label="Waiting for you"
        scope="Now"
        value={awaiting.length}
        sub={oldest ? <>oldest <Timestamp value={oldest} /></> : 'Nothing waiting'}
        tone={awaiting.length ? 'warn' : undefined}
      />
      <Vital
        label="Auto-resolved"
        scope="All time"
        value={incidents ? incidents.autonomous_resolutions : '—'}
        sub={incidents && incidents.total_incidents ? `of ${incidents.total_incidents} incidents (${formatPercent(incidents.autonomous_resolutions / incidents.total_incidents, '—').replace('.0%', '%')})` : 'No incidents yet'}
      />
      <Vital
        label="Avg. time to resolve"
        scope="All time"
        value={performance?.avg_incident_resolution_seconds != null ? formatSpan(performance.avg_incident_resolution_seconds) : '—'}
        sub={performance?.avg_time_to_remediation_seconds != null ? `${formatSpan(performance.avg_time_to_remediation_seconds)} to act` : 'No resolved incidents yet'}
      />
      <Vital
        label="Action success rate"
        scope="All time"
        value={performance?.remediation_success_rate != null ? formatPercent(performance.remediation_success_rate).replace('.0%', '%') : '—'}
        sub={performance?.sample_size ? `${performance.sample_size.executed_attempts} executed actions` : 'No actions yet'}
        hint="Share of executed remediation actions whose recovery validation passed."
      />
    </dl>
  )
}
