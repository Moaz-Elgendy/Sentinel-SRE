import { Radio, WifiOff } from 'lucide-react'
import { useSyncExternalStore } from 'react'
import { getStreamState, subscribeToStreamState } from '@/api/events'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { LiveDot } from './StatusBadge.jsx'
import { TONE_TEXT } from './tone.js'

const COPY = {
  open: { tone: 'ok', label: 'Live', detail: 'Connected to Sentinel’s real-time event stream. Changes appear instantly.' },
  connecting: { tone: 'warn', label: 'Connecting', detail: 'Opening the real-time event stream…' },
  reconnecting: {
    tone: 'warn',
    label: 'Reconnecting',
    detail: 'The event stream dropped and is retrying. Data still refreshes on a timer in the meantime.',
  },
  closed: { tone: 'bad', label: 'Offline', detail: 'The event stream is closed. Data refreshes on a timer only.' },
  idle: { tone: 'neutral', label: 'Idle', detail: 'No live view is open.' },
}

export function LiveIndicator({ className }) {
  const state = useSyncExternalStore(subscribeToStreamState, getStreamState)
  const copy = COPY[state] ?? COPY.idle
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className={cn('inline-flex items-center gap-1.5 text-xs font-medium', TONE_TEXT[copy.tone], className)}>
          {state === 'open' ? <LiveDot /> : state === 'closed' ? <WifiOff aria-hidden="true" className="size-3.5" /> : <Radio aria-hidden="true" className="size-3.5" />}
          <span className="hidden sm:inline">{copy.label}</span>
          <span className="sr-only sm:hidden">{copy.label}</span>
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-60">{copy.detail}</TooltipContent>
    </Tooltip>
  )
}
