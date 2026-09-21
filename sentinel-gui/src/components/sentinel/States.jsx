import { CircleAlert, RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'
import { TONE_SURFACE, TONE_TEXT } from './tone.js'

export function EmptyState({ icon: Icon, title, description, action, className, compact = false }) {
  return (
    <div className={cn('flex flex-col items-center justify-center text-center', compact ? 'gap-1.5 px-4 py-8' : 'gap-2 px-6 py-14', className)}>
      {Icon && (
        <span className="mb-1 grid size-9 place-items-center rounded-md border bg-muted/40 text-muted-foreground">
          <Icon aria-hidden="true" className="size-4.5" />
        </span>
      )}
      <p className="text-sm font-medium">{title}</p>
      {description && <p className="max-w-md text-xs text-muted-foreground">{description}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  )
}

export function ErrorState({ title = 'Could not load this', message, onRetry, className }) {
  return (
    <div role="alert" className={cn('flex flex-col items-center gap-2 px-6 py-12 text-center', className)}>
      <span className={cn('grid size-9 place-items-center rounded-md border', TONE_SURFACE.bad, TONE_TEXT.bad)}>
        <CircleAlert aria-hidden="true" className="size-4.5" />
      </span>
      <p className="text-sm font-medium">{title}</p>
      {message && <p className="max-w-lg text-xs text-muted-foreground">{message}</p>}
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry} className="mt-1">
          <RefreshCw /> Try again
        </Button>
      )}
    </div>
  )
}

/** Inline, tinted message with icon and optional action — for warnings and results. */
export function Callout({ tone = 'neutral', icon: Icon, title, children, action, className }) {
  return (
    <div role={tone === 'bad' ? 'alert' : 'status'} className={cn('flex items-start gap-3 rounded-lg border px-3.5 py-3', TONE_SURFACE[tone], className)}>
      {Icon && <Icon aria-hidden="true" className={cn('mt-0.5 size-4 shrink-0', TONE_TEXT[tone])} />}
      <div className="min-w-0 flex-1 text-sm">
        {title && <p className="font-medium">{title}</p>}
        {children && <div className={cn('text-muted-foreground', title && 'mt-0.5')}>{children}</div>}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  )
}

export function SkeletonRows({ rows = 5, className }) {
  return (
    <div className={cn('space-y-2.5 p-4', className)} aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-8 w-full" style={{ opacity: 1 - i * 0.12 }} />
      ))}
    </div>
  )
}
