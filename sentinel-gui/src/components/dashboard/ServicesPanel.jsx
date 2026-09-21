import { Server } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { Panel } from '../sentinel/Panel.jsx'
import { EmptyState } from '../sentinel/States.jsx'
import { StatusIcon } from '../sentinel/StatusBadge.jsx'
import { TONE_TEXT } from '../sentinel/tone.js'

const healthKey = (healthy) => (healthy === true ? 'healthy' : healthy === false ? 'down' : 'unknown')
const healthWord = { healthy: 'Healthy', down: 'Unhealthy', unknown: 'Unknown' }
const healthTone = { healthy: 'ok', down: 'bad', unknown: 'neutral' }

export function ServicesPanel({ services, loading }) {
  // When every service reports the same reason (e.g. "Kubernetes is unreachable"),
  // say it once instead of repeating it on every row.
  const details = new Set(services.map((s) => s.detail).filter(Boolean))
  const sharedDetail = details.size === 1 && services.length > 1 ? [...details][0] : null

  return (
    <Panel title="Services" icon={Server} description="Current health of each monitored workload" flush>
      {loading ? null : services.length === 0 ? (
        <EmptyState compact title="No services configured" description="Add the application profile to the environment to monitor its services." />
      ) : (
        <>
          {sharedDetail && <p className="border-b bg-muted/30 px-4 py-2 text-xs text-muted-foreground">{sharedDetail}</p>}
          <ul className="divide-y">
            {services.map((service) => {
              const key = healthKey(service.healthy)
              const tone = healthTone[key]
              return (
                <li key={service.name} className="flex items-center gap-3 px-4 py-2">
                  <StatusIcon status={key} className={cn('size-4 shrink-0', TONE_TEXT[tone])} />
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium">{service.name}</p>
                    {!sharedDetail && service.detail && <p className="truncate text-xs text-muted-foreground">{service.detail}</p>}
                  </div>
                  {!service.remediable && (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Badge variant="outline" className="text-muted-foreground">
                          Watch only
                        </Badge>
                      </TooltipTrigger>
                      <TooltipContent className="max-w-56">Sentinel monitors this but never remediates it autonomously.</TooltipContent>
                    </Tooltip>
                  )}
                  <span className={cn('w-16 text-right text-xs font-medium', TONE_TEXT[tone])}>{healthWord[key]}</span>
                </li>
              )
            })}
          </ul>
        </>
      )}
    </Panel>
  )
}
