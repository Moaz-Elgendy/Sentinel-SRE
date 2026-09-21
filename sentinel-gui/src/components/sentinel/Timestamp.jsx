import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useTick } from '@/hooks/useTick'
import { formatRelativeTime, formatTimestamp } from '@/utils/format'

/** Relative time that stays fresh, with the absolute timestamp one hover away. */
export function Timestamp({ value, className, prefix }) {
  useTick()
  if (value == null) return <span className={className}>—</span>
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <time dateTime={new Date(value * 1000).toISOString()} className={className}>
          {prefix}
          {formatRelativeTime(value)}
        </time>
      </TooltipTrigger>
      <TooltipContent>{formatTimestamp(value)}</TooltipContent>
    </Tooltip>
  )
}
