import { ChevronDown, ExternalLink, Repeat } from 'lucide-react'
import { Link } from 'react-router-dom'
import { CopyButton } from '@/components/sentinel/CopyButton'
import { KeyValue } from '@/components/sentinel/KeyValue'
import { Panel } from '@/components/sentinel/Panel'
import { StatusIcon } from '@/components/sentinel/StatusBadge'
import { Timestamp } from '@/components/sentinel/Timestamp'
import { TONE_TEXT } from '@/components/sentinel/tone'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { cn } from '@/lib/utils'
import { formatSpan, formatTimestamp } from '@/utils/format'
import { durationLabel, incidentDuration } from '@/utils/incident'

function NotificationRow({ name, result }) {
  const ok = result?.created === true || result?.sent === true
  const skipped = result?.skipped === true
  const status = ok ? 'healthy' : skipped ? 'unknown' : 'down'
  const tone = ok ? 'ok' : skipped ? 'neutral' : 'bad'
  return (
    <li className="flex items-start gap-2 py-1.5 text-sm first:pt-0 last:pb-0">
      <StatusIcon status={status} className={cn('mt-0.5 size-3.5 shrink-0', TONE_TEXT[tone])} />
      <div className="min-w-0 flex-1">
        <p className="font-medium">
          {name}
          <span className={cn('ml-2 text-xs font-normal', TONE_TEXT[tone])}>{ok ? (name === 'GitHub' ? 'Issue created' : 'Sent') : skipped ? 'Not configured' : 'Failed'}</span>
        </p>
        {result?.url && (
          <a href={result.url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-xs underline underline-offset-2">
            {result.number ? `Issue #${result.number}` : 'Open'} <ExternalLink className="size-3" />
          </a>
        )}
        {!ok && result?.detail && <p className="text-xs break-words text-muted-foreground">{result.detail}</p>}
      </div>
    </li>
  )
}

/** Right rail: identifying facts, recurrence, where the report went, and the raw labels. */
export function FactsPanel({ incident, now }) {
  const notes = incident.notifications ?? {}
  const hasNotes = notes.github || notes.slack || notes.error
  const recurring = (incident.occurrence ?? 1) > 1 || incident.previous_incident_id
  const labels = { ...(incident.labels ?? {}) }
  const annotations = incident.annotations ?? {}

  return (
    <div className="space-y-4">
      <Panel title="Facts">
        <KeyValue
          items={[
            { label: 'Service', value: incident.app },
            { label: 'Namespace', value: incident.namespace, mono: true },
            {
              label: 'Pod',
              value: incident.pod && (
                <span className="inline-flex items-center gap-1">
                  <span className="break-all">{incident.pod}</span>
                  <CopyButton value={incident.pod} label="Copy pod name" />
                </span>
              ),
              mono: true,
            },
            { label: 'Alert', value: incident.alertname },
            { label: 'Started', value: <span className="tnum">{formatTimestamp(incident.created_at)}</span> },
            { label: durationLabel(incident), value: <span className="tnum">{formatSpan(incidentDuration(incident, now / 1000))}</span> },
            { label: 'Closed', value: incident.resolved_at && <span className="tnum">{formatTimestamp(incident.resolved_at)}</span> },
            { label: 'Alert firings', value: incident.firing_count > 1 ? incident.firing_count : null },
            { label: 'Repeats suppressed', value: incident.suppressed_repeats > 0 ? incident.suppressed_repeats : null },
            { label: 'Re-opened', value: incident.reopen_count > 0 ? `${incident.reopen_count}×` : null },
            { label: 'Environment', value: incident.environment_id, mono: true },
          ]}
        />
      </Panel>

      {recurring && (
        <Panel title="Recurrence" icon={Repeat}>
          <p className="text-sm">
            This is occurrence <span className="tnum font-medium">#{incident.occurrence}</span> of this alert on {incident.app}.
          </p>
          {incident.previous_incident_id && (
            <p className="mt-1.5 text-sm">
              Previous:{' '}
              <Link to={`/incidents/${incident.previous_incident_id}`} className="font-mono text-xs underline underline-offset-2">
                {incident.previous_incident_id}
              </Link>
            </p>
          )}
        </Panel>
      )}

      {hasNotes && (
        <Panel title="Notifications" description="Where Sentinel sent its report">
          <ul className="divide-y">
            {notes.github && <NotificationRow name="GitHub" result={notes.github} />}
            {notes.slack && <NotificationRow name="Slack" result={notes.slack} />}
            {notes.error && <li className="py-1.5 text-xs text-bad">{notes.error}</li>}
          </ul>
        </Panel>
      )}

      {(Object.keys(labels).length > 0 || Object.keys(annotations).length > 0) && (
        <Collapsible className="rounded-lg border bg-card">
          <CollapsibleTrigger className="group flex w-full items-center justify-between px-4 py-2.5 text-sm font-semibold outline-none focus-visible:ring-2 focus-visible:ring-ring">
            Alert labels and annotations
            <ChevronDown aria-hidden="true" className="size-4 text-muted-foreground transition-transform group-data-[state=open]:rotate-180" />
          </CollapsibleTrigger>
          <CollapsibleContent className="border-t px-4 py-3">
            <KeyValue mono items={[...Object.entries(labels), ...Object.entries(annotations).map(([k, v]) => [`annotation.${k}`, v])].map(([label, value]) => ({ label, value: String(value) }))} />
          </CollapsibleContent>
        </Collapsible>
      )}
      <p className="px-1 text-xs text-muted-foreground">
        Last updated <Timestamp value={incident.updated_at} />
      </p>
    </div>
  )
}
