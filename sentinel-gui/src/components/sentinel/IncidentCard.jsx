import { Link } from 'react-router-dom'
import { useNow } from '@/hooks/useNow'
import { usePhaseMeta } from '@/hooks/usePhaseMeta'
import { cn } from '@/lib/utils'
import { formatSpan } from '@/utils/format'
import {
  incidentDuration,
  incidentHeadline,
  incidentStatus,
  isActiveIncident,
  isAwaitingHuman,
  isDiagnosed,
  lastEvent,
  rootCauseLabel,
} from '@/utils/incident'
import { LifecycleRail } from './LifecycleRail.jsx'
import { SeverityBadge } from './SeverityBadge.jsx'
import { StatusBadge } from './StatusBadge.jsx'
import { Timestamp } from './Timestamp.jsx'
import { TONE_BORDER_L } from './tone.js'

/**
 * One incident as a scannable row: what it is, where it is in the lifecycle,
 * what Sentinel is doing (or waiting on) right now, and how long it has taken.
 * The whole row is the link, so it works for mouse, keyboard and touch alike.
 */
export function IncidentCard({ incident, className }) {
  const meta = usePhaseMeta()
  const awaiting = isAwaitingHuman(incident)
  const active = isActiveIncident(incident)
  const now = useNow(active ? 1000 : 60000)
  const event = lastEvent(incident)
  const hypothesis = incident.hypothesis
  const phaseLabel = event ? (meta.labels?.[event.phase] ?? event.phase) : null

  const line = awaiting
    ? (incident.escalation_detail ?? 'Waiting for an SRE to decide.')
    : event
      ? `${phaseLabel}: ${event.message}`
      : 'Starting…'

  return (
    <Link
      to={`/incidents/${incident.id}`}
      className={cn(
        'group block border-l-2 px-4 py-3 outline-none transition-colors hover:bg-muted/40 focus-visible:bg-muted/60 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset',
        awaiting ? TONE_BORDER_L.warn : active ? TONE_BORDER_L.info : 'border-l-transparent',
        className
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
            <SeverityBadge severity={incident.severity} iconOnly />
            <span className="font-medium">{incident.alertname}</span>
            <span className="text-muted-foreground">on {incident.app ?? 'unknown'}</span>
            <span className="font-mono text-xs text-muted-foreground">{incident.id}</span>
          </div>
          <p className="mt-0.5 truncate text-xs text-muted-foreground">{incidentHeadline(incident)}</p>
        </div>
        <StatusBadge status={incidentStatus(incident)} className="shrink-0" />
      </div>

      <div className="mt-2.5 grid items-center gap-x-4 gap-y-1.5 sm:grid-cols-[11rem_minmax(0,1fr)_auto]">
        <LifecycleRail incident={incident} variant="mini" />
        <p className="min-w-0 truncate text-xs">
          {isDiagnosed(incident) && (
            <span className="mr-2.5 inline-block border-r pr-2.5 text-muted-foreground">
              {rootCauseLabel(hypothesis.root_cause)} <span className="tnum">{Math.round(hypothesis.confidence * 100)}%</span>
            </span>
          )}
          {line}
        </p>
        <span className="tnum text-xs whitespace-nowrap text-muted-foreground">
          {awaiting ? <Timestamp value={incident.updated_at} prefix="waiting " className="text-warn" /> : active ? `${formatSpan(incidentDuration(incident, now / 1000))} elapsed` : <Timestamp value={incident.created_at} />}
        </span>
      </div>
    </Link>
  )
}
