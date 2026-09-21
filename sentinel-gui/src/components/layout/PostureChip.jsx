import { CircleHelp } from 'lucide-react'
import { Link } from 'react-router-dom'
import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { POSTURE_ICONS, usePosture } from '@/hooks/usePosture'
import { LiveDot } from '../sentinel/StatusBadge.jsx'

export function PostureChip() {
  const posture = usePosture()
  const Icon = POSTURE_ICONS[posture.icon] ?? CircleHelp
  const to = posture.key === 'attention' ? '/incidents?view=attention' : posture.key === 'active' ? '/incidents?view=active' : '/'
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Link to={to} className="rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring">
          <Badge variant={posture.tone} className="h-6 gap-1.5 px-2 text-xs">
            <Icon />
            <span>{posture.chip}</span>
            {posture.key === 'active' && <LiveDot />}
          </Badge>
        </Link>
      </TooltipTrigger>
      <TooltipContent className="max-w-64">
        <p className="font-medium">{posture.headline}</p>
        <p className="opacity-70">{posture.detail}</p>
      </TooltipContent>
    </Tooltip>
  )
}
