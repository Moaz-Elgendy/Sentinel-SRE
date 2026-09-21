import { ArrowUpRight } from 'lucide-react'
import { Link } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { incidentHeadline } from '@/utils/incident'
import { POSTURE_ICONS, usePosture } from '@/hooks/usePosture'
import { LiveDot } from '../sentinel/StatusBadge.jsx'
import { TONE_BORDER_L, TONE_SURFACE, TONE_TEXT } from '../sentinel/tone.js'

/**
 * The first thing on the page and the answer to "do I need to do anything?".
 * Same derivation as the topbar chip (see utils/posture.js), so they always agree.
 */
export function PostureBanner({ environment }) {
  const posture = usePosture()
  const Icon = POSTURE_ICONS[posture.icon]
  const focus = posture.focus

  return (
    <section aria-labelledby="posture-heading" className={cn('flex flex-wrap items-center gap-x-5 gap-y-3 rounded-lg border border-l-4 bg-card px-4 py-3.5', TONE_BORDER_L[posture.tone])}>
      <span className={cn('grid size-10 shrink-0 place-items-center rounded-md border', TONE_SURFACE[posture.tone], TONE_TEXT[posture.tone])}>
        <Icon aria-hidden="true" className="size-5" />
      </span>
      <div className="min-w-0 flex-1 basis-72">
        <h1 id="posture-heading" className="flex items-center gap-2 text-lg leading-6 font-semibold tracking-tight">
          {posture.headline}
          {posture.key === 'active' && <LiveDot className={TONE_TEXT.info} />}
        </h1>
        <p className="mt-0.5 text-sm text-muted-foreground">{posture.detail}</p>
        {focus && (
          <p className="mt-1.5 truncate text-sm">
            <span className="font-medium">{focus.alertname}</span> <span className="text-muted-foreground">on {focus.app}: {incidentHeadline(focus)}</span>
          </p>
        )}
        {!focus && environment && (
          <p className="mt-1 text-xs text-muted-foreground">
            {environment.name} <span aria-hidden="true">/</span> {environment.customer_id}
          </p>
        )}
      </div>
      {focus && (
        <Button asChild variant={posture.key === 'attention' ? 'default' : 'outline'}>
          <Link to={posture.count > 1 ? `/incidents?view=${posture.key === 'attention' ? 'attention' : 'active'}` : `/incidents/${focus.id}`}>
            {posture.key === 'attention'
              ? posture.count > 1
                ? `Review ${posture.count} incidents`
                : 'Review incident'
              : posture.count > 1
                ? `Watch ${posture.count} live`
                : 'Watch it live'}
            <ArrowUpRight />
          </Link>
        </Button>
      )}
    </section>
  )
}
