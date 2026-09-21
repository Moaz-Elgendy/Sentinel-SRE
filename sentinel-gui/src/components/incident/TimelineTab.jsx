import { ChevronRight } from 'lucide-react'
import { useState } from 'react'
import { Panel } from '@/components/sentinel/Panel'
import { PhaseIcon } from '@/components/sentinel/PhaseIcon'
import { EmptyState } from '@/components/sentinel/States'
import { TONE_TEXT } from '@/components/sentinel/tone'
import { usePhaseMeta } from '@/hooks/usePhaseMeta'
import { cn } from '@/lib/utils'
import { formatClock, formatSpan, formatTimestamp } from '@/utils/format'
import { escalationReasonLabel } from '@/utils/labels'
import { History } from 'lucide-react'

function stringify(value) {
  return typeof value === 'object' && value !== null ? JSON.stringify(value, null, 2) : String(value)
}

function Row({ event, previous, label, incidentStart }) {
  const [open, setOpen] = useState(false)
  const detail = Object.entries(event.detail ?? {}).filter(([, v]) => v != null && v !== '')
  const hasDetail = detail.length > 0
  const escalation = event.phase === 'escalation'
  const gap = previous ? event.at - previous.at : event.at - incidentStart
  return (
    <li className="grid grid-cols-[4.5rem_1.25rem_minmax(0,1fr)_3.75rem] items-start gap-x-3 px-4 py-2.5">
      <time className="tnum pt-0.5 font-mono text-xs whitespace-nowrap text-muted-foreground" title={formatTimestamp(event.at)} dateTime={new Date(event.at * 1000).toISOString()}>
        {formatClock(event.at)}
      </time>
      <PhaseIcon phase={event.phase} className={cn('mt-0.5 size-4', escalation ? TONE_TEXT.warn : 'text-muted-foreground')} />
      <div className="min-w-0">
        <p className="text-xs text-muted-foreground">{label}</p>
        <p className="text-sm break-words">{event.message}</p>
        {hasDetail && (
          <>
            <button type="button" onClick={() => setOpen((o) => !o)} aria-expanded={open} className="mt-1 inline-flex items-center gap-1 text-xs text-muted-foreground outline-none hover:text-foreground focus-visible:underline">
              <ChevronRight aria-hidden="true" className={cn('size-3 transition-transform', open && 'rotate-90')} /> {open ? 'Hide' : 'Show'} recorded detail
            </button>
            {open && (
              <dl className="mt-1.5 grid gap-x-4 gap-y-1 rounded-md border bg-muted/30 p-2.5 text-xs sm:grid-cols-[auto_minmax(0,1fr)]">
                {detail.map(([k, v]) => (
                  <div key={k} className="contents">
                    <dt className="text-muted-foreground">{k}</dt>
                    <dd className="font-mono break-words whitespace-pre-wrap">{stringify(v)}</dd>
                  </div>
                ))}
              </dl>
            )}
          </>
        )}
      </div>
      <span className="tnum pt-0.5 text-right text-xs text-muted-foreground" title="Time since the previous step">
        +{formatSpan(gap)}
      </span>
    </li>
  )
}

/** The full audit trail: every step Sentinel recorded, in order, with recorded detail on demand. */
export function TimelineTab({ incident }) {
  const meta = usePhaseMeta()
  const timeline = incident.timeline ?? []
  const history = incident.escalation_history ?? []
  return (
    <div className="space-y-4">
      <Panel title="Audit trail" description={`${timeline.length} step${timeline.length === 1 ? '' : 's'} recorded by Sentinel`} flush>
        {timeline.length === 0 ? (
          <EmptyState compact icon={History} title="Nothing recorded yet" />
        ) : (
          <ol className="divide-y">
            {timeline.map((event, i) => (
              <Row key={`${event.at}-${i}`} event={event} previous={timeline[i - 1]} label={meta.labels?.[event.phase] ?? event.phase} incidentStart={incident.created_at} />
            ))}
          </ol>
        )}
      </Panel>
      {history.length > 0 && (
        <Panel title="Earlier escalations" description="This incident was escalated before and re-opened" flush>
          <ul className="divide-y">
            {history.map((record, i) => (
              <li key={`${record.at}-${i}`} className="px-4 py-2.5 text-sm">
                <p className="font-medium">{escalationReasonLabel(record.reason)}</p>
                <p className="text-xs text-muted-foreground">{formatTimestamp(record.at)}</p>
                {record.detail && <p className="mt-1 text-sm">{record.detail}</p>}
              </li>
            ))}
          </ul>
        </Panel>
      )}
    </div>
  )
}
