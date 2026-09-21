import {
  Activity,
  CircleCheck,
  CircleDashed,
  CircleDot,
  CircleHelp,
  CircleX,
  Clock,
  Hand,
  OctagonAlert,
  Search,
  ShieldCheck,
  TriangleAlert,
  Wrench,
} from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'
import { isLiveStatus, statusKey, statusLabel, statusTone } from '@/utils/status'

// Every status pairs a hue with a distinct icon and a text label, so no state
// is ever communicated by colour alone.
const ICONS = {
  operational: CircleCheck,
  healthy: CircleCheck,
  monitoring: CircleCheck,
  degraded: TriangleAlert,
  critical: OctagonAlert,
  down: OctagonAlert,
  unknown: CircleHelp,
  true: CircleCheck,
  false: OctagonAlert,
  null: CircleHelp,
  open: CircleDot,
  investigating: Search,
  remediating: Wrench,
  validating: ShieldCheck,
  resolved: CircleCheck,
  auto_resolved: CircleCheck,
  escalated: Hand,
  applied: CircleCheck,
  succeeded: CircleCheck,
  passed: CircleCheck,
  success: CircleCheck,
  failed: CircleX,
  timedout: CircleX,
  pending: CircleDashed,
  cancelled: CircleDashed,
  cancelling: CircleDashed,
  delayed: Clock,
  inprogress: Activity,
}

export function StatusIcon({ status, className }) {
  const Icon = ICONS[statusKey(status)] ?? CircleHelp
  return <Icon aria-hidden="true" className={className} />
}

/** Live ring: motion that only ever means "Sentinel is working on this right now". */
export function LiveDot({ className }) {
  return (
    <span
      aria-hidden="true"
      className={cn('size-1.5 shrink-0 rounded-full bg-current motion-safe:animate-live-ring', className)}
    />
  )
}

export function StatusBadge({ status, label, className }) {
  const tone = statusTone(status)
  return (
    <Badge variant={tone} className={cn('gap-1', className)} data-status={statusKey(status)}>
      <StatusIcon status={status} />
      <span>{label ?? statusLabel(status)}</span>
      {isLiveStatus(status) && <LiveDot />}
    </Badge>
  )
}
