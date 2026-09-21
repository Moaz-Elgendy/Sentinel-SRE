import { cn } from '@/lib/utils'

/** Compact definition list: label left, value right, hairline rows. */
export function KeyValue({ items, className, mono = false }) {
  const visible = items.filter((item) => item && item.value !== undefined && item.value !== null && item.value !== false)
  return (
    <dl className={cn('divide-y text-sm', className)}>
      {visible.map((item) => (
        <div key={item.label} className="flex items-baseline justify-between gap-4 py-1.5 first:pt-0 last:pb-0">
          <dt className="shrink-0 text-xs text-muted-foreground">{item.label}</dt>
          <dd className={cn('min-w-0 text-right break-words', (mono || item.mono) && 'font-mono text-xs')}>{item.value}</dd>
        </div>
      ))}
    </dl>
  )
}
