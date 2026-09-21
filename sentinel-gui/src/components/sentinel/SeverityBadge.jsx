import { Info, OctagonAlert, TriangleAlert } from 'lucide-react'
import { cn } from '@/lib/utils'
import { TONE_TEXT } from './tone.js'

const SEVERITY = {
  critical: { label: 'Critical', tone: 'bad', Icon: OctagonAlert },
  warning: { label: 'Warning', tone: 'warn', Icon: TriangleAlert },
  info: { label: 'Info', tone: 'info', Icon: Info },
}

/** Icon + word, no chip: severity is a property of the alert, not a state to shout about. */
export function SeverityBadge({ severity, className, iconOnly = false }) {
  const config = SEVERITY[String(severity).toLowerCase()] ?? { label: severity ?? 'Unknown', tone: 'neutral', Icon: Info }
  const { Icon } = config
  return (
    <span className={cn('inline-flex items-center gap-1 text-xs font-medium', TONE_TEXT[config.tone], className)}>
      <Icon aria-hidden="true" className="size-3.5" />
      <span className={cn(iconOnly && 'sr-only')}>{config.label}</span>
    </span>
  )
}
