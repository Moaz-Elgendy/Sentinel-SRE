import { Ban, Check, CircleCheck, Hand, LoaderCircle, Minus, RotateCcw, X } from 'lucide-react'
import { useEffect, useMemo, useRef } from 'react'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useNow } from '@/hooks/useNow'
import { usePhaseMeta } from '@/hooks/usePhaseMeta'
import { cn } from '@/lib/utils'
import { formatClock, formatSpan } from '@/utils/format'
import { isActiveIncident, lifecycleOutcome, lifecycleStages, reinvestigationCount } from '@/utils/incident'

/**
 * Detect → Evidence → Correlate → Diagnose → Decide → Policy → Act → Validate → Document → Outcome
 *
 * The same lifecycle, drawn identically everywhere an incident appears. Stage
 * order and wording come from the real timeline and the backend's phase
 * metadata; nothing here is decorative. Hue is reserved for the exceptions —
 * the stage Sentinel is on now (blue), a policy block (amber), a failure (red)
 * and the outcome — so a healthy path reads as a calm, quiet line.
 */
const SEGMENT = {
  done: 'bg-foreground/40',
  pending: 'bg-border',
  skipped: 'border border-dashed border-border bg-transparent',
  blocked: 'bg-warn-solid',
  failed: 'bg-bad-solid',
  active:
    'bg-[linear-gradient(90deg,var(--info-solid)_0%,color-mix(in_oklab,var(--info-solid)_45%,white)_50%,var(--info-solid)_100%)] bg-[length:200%_100%] motion-safe:animate-live-sweep',
  ok: 'bg-ok-solid',
  attention: 'bg-warn-solid',
}

const STATE_WORD = {
  done: 'Done',
  active: 'In progress',
  pending: 'Not reached yet',
  skipped: 'Not reached',
  blocked: 'Blocked by policy',
  failed: 'Failed',
  ok: 'Done',
  attention: 'Needs a human',
}

function StateIcon({ state, className }) {
  const props = { 'aria-hidden': true, className: cn('size-3 shrink-0', className) }
  switch (state) {
    case 'done':
      return <Check {...props} />
    case 'active':
      return <LoaderCircle {...props} className={cn(props.className, 'text-info motion-safe:animate-spin')} />
    case 'blocked':
      return <Ban {...props} className={cn(props.className, 'text-warn')} />
    case 'failed':
      return <X {...props} className={cn(props.className, 'text-bad')} />
    case 'ok':
      return <CircleCheck {...props} className={cn(props.className, 'text-ok')} />
    case 'attention':
      return <Hand {...props} className={cn(props.className, 'text-warn')} />
    case 'skipped':
      return <Minus {...props} className={cn(props.className, 'text-muted-foreground/60')} />
    default:
      return <span aria-hidden="true" className="size-1.5 shrink-0 rounded-full bg-muted-foreground/30" />
  }
}

function useRail(incident) {
  const meta = usePhaseMeta()
  // Re-derive on a slow clock only for open incidents (the active stage's timer moves).
  const now = useNow(isActiveIncident(incident) ? 1000 : 60000)
  return useMemo(() => {
    const nowSeconds = now / 1000
    return {
      stages: lifecycleStages(incident, meta, nowSeconds),
      outcome: lifecycleOutcome(incident),
      loops: reinvestigationCount(incident),
    }
  }, [incident, meta, now])
}

function summarise(stages, outcome) {
  const active = stages.find((s) => s.state === 'active')
  const problem = stages.find((s) => s.state === 'blocked' || s.state === 'failed')
  if (active) return `${active.fullLabel}: in progress`
  if (problem) return `${problem.fullLabel}: ${STATE_WORD[problem.state].toLowerCase()}`
  return outcome.state === 'pending' ? 'In progress' : outcome.label
}

/** Compact strip for lists and cards; one tooltip lists every stage. */
function MiniRail({ incident, className }) {
  const { stages, outcome } = useRail(incident)
  const label = summarise(stages, outcome)
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <div role="img" aria-label={`Lifecycle: ${label}`} className={cn('flex h-4 w-full min-w-24 max-w-44 items-center gap-0.5', className)}>
          {stages.map((stage) => (
            <span key={stage.id} className={cn('h-1.5 flex-1 rounded-[1px]', SEGMENT[stage.state])} />
          ))}
          <span className={cn('ml-0.5 h-1.5 w-3 rounded-[1px]', SEGMENT[outcome.state])} />
        </div>
      </TooltipTrigger>
      <TooltipContent side="bottom" align="start" className="w-52">
        <ul className="space-y-1">
          {[...stages, { ...outcome, fullLabel: outcome.label }].map((stage) => (
            <li key={stage.id} className="flex items-center gap-2 text-xs">
              <StateIcon state={stage.state} />
              <span className="flex-1">{stage.fullLabel}</span>
              <span className="opacity-70">{stage.state === 'pending' || stage.state === 'skipped' ? '' : STATE_WORD[stage.state]}</span>
            </li>
          ))}
        </ul>
      </TooltipContent>
    </Tooltip>
  )
}

function FullRail({ incident, className }) {
  const { stages, outcome, loops } = useRail(incident)
  const columns = [...stages, { ...outcome, fullLabel: outcome.label, message: null, at: null, seconds: null, isOutcome: true }]
  const scroller = useRef(null)

  // On a narrow screen the rail scrolls sideways; bring the stage that matters
  // (in progress, blocked, failed, or where it ended up) into view instead of the start.
  const focusId = (columns.find((c) => ['active', 'blocked', 'failed', 'attention'].includes(c.state)) ?? columns[columns.length - 1]).id
  useEffect(() => {
    const el = scroller.current
    const target = el?.querySelector('[data-focus="true"]')
    if (!el || !target || el.scrollWidth <= el.clientWidth) return
    el.scrollLeft = Math.max(0, target.offsetLeft - el.clientWidth / 2 + target.offsetWidth / 2)
  }, [focusId])

  return (
    <div ref={scroller} className={cn('relative overflow-x-auto', className)}>
      <ol className="grid min-w-176 grid-cols-10 gap-1.5" aria-label="Incident lifecycle">
        {columns.map((stage) => {
          const dim = stage.state === 'pending' || stage.state === 'skipped'
          return (
            <li key={stage.id} data-focus={stage.id === focusId} className="min-w-0">
              <Tooltip>
                <TooltipTrigger asChild>
                  <button type="button" className="group block w-full rounded-sm text-left outline-offset-4">
                    <span className={cn('block h-1.5 w-full rounded-[2px]', SEGMENT[stage.state])} />
                    <span className={cn('mt-2 flex items-center gap-1 text-xs', dim ? 'text-muted-foreground' : 'font-medium')}>
                      <StateIcon state={stage.state} />
                      <span className="truncate">{stage.label}</span>
                      <span className="sr-only">: {STATE_WORD[stage.state]}</span>
                    </span>
                    <span className="tnum mt-0.5 block truncate pl-4 text-[11px] leading-4 text-muted-foreground">
                      {stage.isOutcome ? '\u00a0' : stage.seconds != null && stage.state !== 'pending' ? formatSpan(stage.seconds) : '\u00a0'}
                    </span>
                  </button>
                </TooltipTrigger>
                <TooltipContent side="bottom" className="max-w-64">
                  <p className="font-medium">{stage.fullLabel}</p>
                  <p className="opacity-70">{STATE_WORD[stage.state]}</p>
                  {stage.at != null && <p className="tnum mt-1 opacity-70">Reached {formatClock(stage.at)}</p>}
                  {stage.message && <p className="mt-1 break-words">{stage.message}</p>}
                </TooltipContent>
              </Tooltip>
            </li>
          )
        })}
      </ol>
      {loops > 0 && (
        <p className="mt-2 inline-flex items-center gap-1.5 text-xs text-muted-foreground">
          <RotateCcw aria-hidden="true" className="size-3" />
          Re-investigated {loops} time{loops === 1 ? '' : 's'} after a failed validation
        </p>
      )}
    </div>
  )
}

export function LifecycleRail({ incident, variant = 'mini', className }) {
  return variant === 'full' ? <FullRail incident={incident} className={className} /> : <MiniRail incident={incident} className={className} />
}
