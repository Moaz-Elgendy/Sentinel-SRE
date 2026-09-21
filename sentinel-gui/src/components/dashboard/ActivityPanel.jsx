import { Activity } from 'lucide-react'
import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import { ScrollArea } from '@/components/ui/scroll-area'
import { usePhaseMeta } from '@/hooks/usePhaseMeta'
import { cn } from '@/lib/utils'
import { formatClock } from '@/utils/format'
import { LiveIndicator } from '../sentinel/LiveIndicator.jsx'
import { Panel } from '../sentinel/Panel.jsx'
import { PhaseIcon } from '../sentinel/PhaseIcon.jsx'
import { EmptyState } from '../sentinel/States.jsx'
import { Timestamp } from '../sentinel/Timestamp.jsx'
import { TONE_TEXT } from '../sentinel/tone.js'

const MAX_ENTRIES = 60

/**
 * Sentinel's running commentary, newest first: every entry is a real timeline
 * event Sentinel recorded on an incident (the same audit trail shown on the
 * incident page). It refreshes the instant the event stream reports a change.
 */
export function ActivityPanel({ incidents }) {
  const meta = usePhaseMeta()
  const entries = useMemo(
    () =>
      incidents
        .flatMap((incident) => (incident.timeline ?? []).map((event) => ({ incident, event })))
        .sort((a, b) => b.event.at - a.event.at)
        .slice(0, MAX_ENTRIES),
    [incidents]
  )

  return (
    <Panel title="Live activity" icon={Activity} flush actions={<LiveIndicator />} description="What Sentinel has been doing, across all incidents">
      {entries.length === 0 ? (
        <EmptyState compact title="No activity yet" description="Sentinel’s reasoning and actions will stream in here as incidents happen." />
      ) : (
        <ScrollArea className="h-80">
          <ol className="divide-y">
            {entries.map(({ incident, event }, i) => {
              const escalation = event.phase === 'escalation'
              return (
                <li key={`${incident.id}-${event.at}-${event.phase}-${i}`} className="grid grid-cols-[4.25rem_1rem_minmax(0,1fr)] items-start gap-x-3 px-4 py-2">
                  <time className="tnum pt-0.5 font-mono text-xs whitespace-nowrap text-muted-foreground" dateTime={new Date(event.at * 1000).toISOString()} title={new Date(event.at * 1000).toLocaleString()}>
                    {formatClock(event.at)}
                  </time>
                  <PhaseIcon phase={event.phase} className={cn('mt-0.5 size-4', escalation ? TONE_TEXT.warn : 'text-muted-foreground')} />
                  <div className="min-w-0">
                    <p className="text-sm break-words">{event.message}</p>
                    <p className="mt-0.5 flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
                      <span>{meta.labels?.[event.phase] ?? event.phase}</span>
                      <Link to={`/incidents/${incident.id}`} className="font-mono underline-offset-2 hover:text-foreground hover:underline">
                        {incident.id}
                      </Link>
                      <span>{incident.alertname}</span>
                      <Timestamp value={event.at} />
                    </p>
                  </div>
                </li>
              )
            })}
          </ol>
        </ScrollArea>
      )}
    </Panel>
  )
}
