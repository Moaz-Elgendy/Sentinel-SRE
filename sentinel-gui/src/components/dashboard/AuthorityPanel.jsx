import { ArrowUpRight, ShieldCheck } from 'lucide-react'
import { useCallback } from 'react'
import { Link } from 'react-router-dom'
import { getPolicyConfig, getRemediationConfig } from '@/api/config'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { usePolling } from '@/hooks/usePolling'
import { Panel } from '../sentinel/Panel.jsx'
import { ErrorState, SkeletonRows } from '../sentinel/States.jsx'

const pct = (v) => `${Math.round(v * 100)}%`

/**
 * "What is Sentinel allowed to do?" — read straight from the live policy and
 * remediation config, so it is the same truth the Policy Engine enforces.
 */
export function AuthorityPanel() {
  const load = useCallback(() => Promise.all([getPolicyConfig(), getRemediationConfig()]), [])
  const { data, error, loading, refetch } = usePolling(load, { intervalMs: 60000 })

  return (
    <Panel
      title="Authority"
      icon={ShieldCheck}
      description="What Sentinel may do without asking"
      actions={
        <Button asChild variant="ghost" size="sm">
          <Link to="/policies">
            Guardrails <ArrowUpRight />
          </Link>
        </Button>
      }
    >
      {loading ? (
        <SkeletonRows rows={3} className="p-0" />
      ) : error && !data ? (
        <ErrorState title="Could not load guardrails" onRetry={refetch} className="py-6" />
      ) : (
        <AuthorityBody policy={data[0].current} protectedScope={data[0].protected} dryRun={data[1].current.dry_run} />
      )}
    </Panel>
  )
}

function Chips({ items }) {
  return (
    <div className="flex flex-wrap justify-end gap-1">
      {items.map((item) => (
        <Badge key={item} variant="secondary" className="font-mono font-normal">
          {item}
        </Badge>
      ))}
    </div>
  )
}

function AuthorityBody({ policy, protectedScope, dryRun }) {
  const actions = [
    ['Restart a deployment', policy.confidence_restart],
    ['Roll back a deployment', policy.confidence_rollback],
    ['Scale a deployment', policy.confidence_scale, `${policy.min_replicas}–${policy.max_replicas} replicas`],
    ['Reset a chaos fault', policy.confidence_chaos_reset],
  ]
  return (
    <div className="space-y-3">
      {dryRun && <p className="rounded-md border border-warn-edge bg-warn-tint px-2.5 py-1.5 text-xs text-warn">Dry run is on: Sentinel records what it would do but changes nothing.</p>}
      <div>
        <p className="mb-1 text-xs text-muted-foreground">Acts on its own when confidence is at least</p>
        <ul className="divide-y rounded-md border">
          {actions.map(([label, threshold, extra]) => (
            <li key={label} className="flex items-center justify-between gap-3 px-2.5 py-1.5 text-sm">
              <span>{label}</span>
              <span className="tnum text-xs text-muted-foreground">
                {extra && <span className="mr-2">{extra}</span>}
                <span className="font-medium text-foreground">{pct(threshold)}</span>
              </span>
            </li>
          ))}
        </ul>
      </div>
      <dl className="divide-y text-sm">
        <div className="flex items-start justify-between gap-4 py-1.5 first:pt-0">
          <dt className="shrink-0 text-xs text-muted-foreground">Namespaces</dt>
          <dd>
            <Chips items={policy.allowed_namespaces} />
          </dd>
        </div>
        <div className="flex items-start justify-between gap-4 py-1.5">
          <dt className="shrink-0 text-xs text-muted-foreground">Deployments</dt>
          <dd>
            <Chips items={policy.allowed_deployments} />
          </dd>
        </div>
        <div className="flex items-start justify-between gap-4 py-1.5">
          <dt className="shrink-0 text-xs text-muted-foreground">Limits</dt>
          <dd className="text-right text-xs">
            {policy.max_actions_per_incident} actions per incident, {policy.action_cooldown_seconds}s apart
          </dd>
        </div>
        {protectedScope?.denied_deployments?.length > 0 && (
          <div className="flex items-start justify-between gap-4 py-1.5 last:pb-0">
            <dt className="shrink-0 text-xs text-muted-foreground">Never touches</dt>
            <dd>
              <Chips items={protectedScope.denied_deployments} />
            </dd>
          </div>
        )}
      </dl>
    </div>
  )
}
