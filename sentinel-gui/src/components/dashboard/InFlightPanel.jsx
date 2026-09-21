import { CircleCheck } from 'lucide-react'
import { Link } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import { Panel } from '../sentinel/Panel.jsx'
import { EmptyState, SkeletonRows } from '../sentinel/States.jsx'
import { IncidentCard } from '../sentinel/IncidentCard.jsx'

function GroupLabel({ children, count }) {
  return (
    <div className="flex items-center justify-between border-b bg-muted/30 px-4 py-1.5 text-xs font-medium text-muted-foreground">
      <span>{children}</span>
      <span className="tnum">{count}</span>
    </div>
  )
}

/** Escalated first (a human is blocking progress), then what Sentinel is handling itself. */
export function InFlightPanel({ awaiting, active, loading, watchersOk }) {
  const activeIds = new Set(awaiting.map((i) => i.id))
  const working = active.filter((i) => !activeIds.has(i.id))
  const empty = awaiting.length === 0 && working.length === 0

  return (
    <Panel
      title="Incidents that need eyes"
      flush
      actions={
        <Button asChild variant="ghost" size="sm">
          <Link to="/incidents">All incidents</Link>
        </Button>
      }
    >
      {loading ? (
        <SkeletonRows rows={2} />
      ) : empty ? (
        <EmptyState
          compact
          icon={CircleCheck}
          title="Nothing needs you right now"
          description={watchersOk === false ? 'Sentinel has no active incidents, but one or more of its data sources is unreachable, so it may not see new problems.' : 'No incidents are active or waiting. Sentinel will open one as soon as an alert fires.'}
        />
      ) : (
        <div>
          {awaiting.length > 0 && (
            <section aria-label="Waiting for you">
              <GroupLabel count={awaiting.length}>Waiting for you</GroupLabel>
              <ul className="divide-y">
                {awaiting.map((incident) => (
                  <li key={incident.id}>
                    <IncidentCard incident={incident} />
                  </li>
                ))}
              </ul>
            </section>
          )}
          {working.length > 0 && (
            <section aria-label="Sentinel is working on these">
              <GroupLabel count={working.length}>Sentinel is working on</GroupLabel>
              <ul className="divide-y">
                {working.map((incident) => (
                  <li key={incident.id}>
                    <IncidentCard incident={incident} />
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
      )}
    </Panel>
  )
}
