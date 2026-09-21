import { Card } from '@/components/ui/card'
import { cn } from '@/lib/utils'

/**
 * The console's one container. A hairline-bordered surface with an optional
 * header row — deliberately flat: no shadows, no nested rounded cards.
 */
export function Panel({ title, description, icon: Icon, actions, children, className, bodyClassName, flush = false, id }) {
  const hasHeader = title || actions
  return (
    <Card id={id} className={cn('gap-0 overflow-hidden', className)}>
      {hasHeader && (
        <header className="flex min-h-11 items-center justify-between gap-3 border-b px-4 py-2.5">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 text-sm leading-5 font-semibold">
              {Icon && <Icon aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />}
              <span className="truncate">{title}</span>
            </h2>
            {description && <p className="mt-0.5 text-xs text-muted-foreground">{description}</p>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={cn(!flush && 'px-4 py-3', bodyClassName)}>{children}</div>
    </Card>
  )
}
