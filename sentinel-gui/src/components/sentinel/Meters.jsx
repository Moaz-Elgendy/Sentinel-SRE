import { cn } from '@/lib/utils'
import { TONE_SOLID, TONE_TEXT } from './tone.js'

/** Confidence as a bar; with a policy threshold it also shows which side of it the value sits on. */
export function ConfidenceMeter({ value, threshold, className, size = 'md' }) {
  if (value == null) return <span className="text-muted-foreground">—</span>
  const pct = Math.max(0, Math.min(1, value)) * 100
  const meets = threshold == null ? null : value >= threshold
  return (
    <div className={cn('flex items-center gap-2.5', className)}>
      <div
        role="meter"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(pct)}
        aria-label="Confidence"
        className={cn('relative w-full min-w-16 overflow-visible rounded-full bg-muted', size === 'sm' ? 'h-1' : 'h-1.5')}
      >
        <div
          className={cn('h-full rounded-full', meets === false ? TONE_SOLID.warn : meets ? TONE_SOLID.ok : 'bg-foreground/60')}
          style={{ width: `${pct}%` }}
        />
        {threshold != null && (
          <span
            aria-hidden="true"
            className="absolute top-1/2 h-3 w-px -translate-y-1/2 bg-foreground/70"
            style={{ left: `${threshold * 100}%` }}
          />
        )}
      </div>
      <span className="tnum shrink-0 text-sm font-medium">{Math.round(pct)}%</span>
    </div>
  )
}

/**
 * One observed signal against the limit Sentinel's RCA uses for it. The bar is
 * scaled so the limit sits at 60% of the track: anything past the marker is
 * visibly "over", and the text says so as well.
 */
export function MetricMeter({ label, display, value, limit, limitDisplay, className }) {
  const hasLimit = limit != null && limit > 0 && value != null
  const over = hasLimit && value > limit
  const max = hasLimit ? limit / 0.6 : null
  const pct = hasLimit ? Math.min(100, (value / max) * 100) : 0
  return (
    <div className={cn('space-y-1.5', className)}>
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-xs text-muted-foreground">{label}</span>
        <span className={cn('tnum text-sm font-medium', over && TONE_TEXT.bad)}>{display}</span>
      </div>
      {hasLimit ? (
        <>
          <div className="relative h-1.5 rounded-full bg-muted">
            <div className={cn('h-full rounded-full', over ? TONE_SOLID.bad : 'bg-foreground/45')} style={{ width: `${pct}%` }} />
            <span aria-hidden="true" className="absolute top-1/2 h-3 w-px -translate-y-1/2 bg-foreground/70" style={{ left: '60%' }} />
          </div>
          <p className={cn('text-[11px]', over ? TONE_TEXT.bad : 'text-muted-foreground')}>
            {over ? `Over the ${limitDisplay} limit` : `Within the ${limitDisplay} limit`}
          </p>
        </>
      ) : (
        <div className="h-1.5 rounded-full bg-muted/60" aria-hidden="true" />
      )}
    </div>
  )
}
