import { Bot } from 'lucide-react'
import { useActivity } from '@/context/ActivityContext'
import { cn } from '@/lib/utils'
import { formatSpan } from '@/utils/format'
import { Panel } from '../sentinel/Panel.jsx'
import { StatusIcon } from '../sentinel/StatusBadge.jsx'
import { Timestamp } from '../sentinel/Timestamp.jsx'
import { TONE_TEXT } from '../sentinel/tone.js'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'

function Row({ label, children }) {
  return (
    <div className="flex items-start justify-between gap-4 py-2 first:pt-0 last:pb-0">
      <dt className="shrink-0 text-xs text-muted-foreground">{label}</dt>
      <dd className="min-w-0 text-right text-sm">{children}</dd>
    </div>
  )
}

const REASONER = {
  healthy: { tone: 'ok', word: 'Healthy' },
  unavailable: { tone: 'warn', word: 'Unavailable' },
  unknown: { tone: 'neutral', word: 'Not called yet' },
  not_configured: { tone: 'neutral', word: 'Not configured' },
}

/** Sentinel's own state: how it decides, what it can currently see, when it last looked. */
export function SentinelPanel({ summary }) {
  const { status, watchers } = useActivity()
  const sentinel = summary?.sentinel
  const reasoner = status?.reasoner
  const reasonerView = reasoner ? (REASONER[reasoner.status] ?? REASONER.unknown) : null

  return (
    <Panel title="Sentinel" icon={Bot} description="How it decides and what it can see">
      <dl className="divide-y">
        <Row label="Mode">
          {sentinel ? (sentinel.mode === 'dry_run' ? <span className={TONE_TEXT.warn}>Dry run, no changes applied</span> : 'Autonomous') : '—'}
        </Row>
        <Row label="Reasoning">
          {sentinel ? (sentinel.llm === 'enabled' ? 'Rules + LLM' : 'Rules only') : '—'}
        </Row>
        {sentinel?.llm === 'enabled' && reasonerView && (
          <Row label="LLM provider">
            <Tooltip>
              <TooltipTrigger asChild>
                <span className={cn('inline-flex items-center gap-1.5', TONE_TEXT[reasonerView.tone])}>
                  <StatusIcon status={reasoner.status === 'healthy' ? 'healthy' : reasoner.status === 'unavailable' ? 'degraded' : 'unknown'} className="size-3.5" />
                  {reasoner.provider ? `${reasoner.provider}: ` : ''}
                  {reasonerView.word}
                </span>
              </TooltipTrigger>
              <TooltipContent className="max-w-72">
                {reasoner.status === 'unavailable'
                  ? `Sentinel falls back to rules-only analysis while the provider is down.${reasoner.last_error ? ` Last error: ${reasoner.last_error}` : ''}${reasoner.retry_after_seconds > 0 ? ` Retrying in ${formatSpan(reasoner.retry_after_seconds)}.` : ''}`
                  : 'Health of the LLM provider Sentinel consults for diagnosis. It is a condition, never an incident.'}
              </TooltipContent>
            </Tooltip>
          </Row>
        )}
        <Row label="Data sources">
          {watchers ? (
            <ul className="space-y-1">
              {watchers.items.map((w) => (
                <li key={w.name} className="flex items-center justify-end gap-1.5 text-sm">
                  <span>{w.name}</span>
                  <StatusIcon status={w.connected ? 'healthy' : 'down'} className={cn('size-3.5', w.connected ? TONE_TEXT.ok : TONE_TEXT.bad)} />
                  <span className={cn('w-20 text-left text-xs', w.connected ? 'text-muted-foreground' : TONE_TEXT.bad)}>{w.connected ? 'Connected' : 'Unreachable'}</span>
                </li>
              ))}
              <li className="text-xs text-muted-foreground">
                Checked <Timestamp value={watchers.at / 1000} />
              </li>
            </ul>
          ) : (
            <span className="text-xs text-muted-foreground">
              {status ? 'Checked only while Sentinel is idle' : 'Checking…'}
            </span>
          )}
        </Row>
        <Row label="Last incident">{summary?.incidents?.last_incident_at ? <Timestamp value={summary.incidents.last_incident_at} /> : 'None yet'}</Row>
      </dl>
    </Panel>
  )
}
