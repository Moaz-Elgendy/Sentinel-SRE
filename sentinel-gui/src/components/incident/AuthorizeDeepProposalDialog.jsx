import { ShieldCheck } from 'lucide-react'
import { useState } from 'react'
import { authorizeDeepProposal } from '@/api/authorizations'
import { extractErrorMessage } from '@/api/client'
import { ConfirmDialog } from '@/components/sentinel/ConfirmDialog'
import { Callout } from '@/components/sentinel/States'
import { Badge } from '@/components/ui/badge'
import { notify } from '@/lib/notify'
import { describeNovelActionTarget, novelActionLabel } from '@/utils/labels'

/**
 * Authorizes exactly one Deep Investigation proposal. Unlike AuthorizeDialog
 * (which lets an SRE pick from four known actions), there is nothing to
 * choose here — the proposal already names its own action_type and target;
 * this dialog only confirms that specific, already-fully-specified change.
 *
 * The wording matches what the backend actually does
 * (routers/authorizations.py's deep-proposals endpoint /
 * Orchestrator.authorize_and_remediate_deep): scoped to exactly this
 * proposal, single-use, and — unlike the known-action path — there is no
 * confidence waiver at all here (PolicyEngine.evaluate_deep_proposal has its
 * own fixed, higher confidence floor with no override parameter), so this
 * dialog never mentions "waiving" anything.
 */
export function AuthorizeDeepProposalDialog({ incident, proposal, onOpenChange, onAuthorized }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const open = Boolean(proposal)

  async function confirm() {
    setBusy(true)
    setError(null)
    try {
      await authorizeDeepProposal(incident.id, proposal.id)
      notify.success('Deep Investigation proposal authorized', {
        description: 'Sentinel is executing it now. Follow progress on this page.',
      })
      onOpenChange(null)
      onAuthorized?.()
    } catch (err) {
      setError(extractErrorMessage(err, 'Could not submit the authorization. Nothing was changed.'))
    } finally {
      setBusy(false)
    }
  }

  if (!proposal) return null
  const target = proposal.target ?? {}
  const targetLine = [target.deployment, target.namespace && `in ${target.namespace}`].filter(Boolean).join(' ')
  const change = describeNovelActionTarget(proposal.action_type, target)

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          setError(null)
          onOpenChange(null)
        }
      }}
      title={`Authorize: ${novelActionLabel(proposal.action_type).toLowerCase()}`}
      description="You are granting Sentinel permission to run this one novel action, on this one incident, exactly once."
      confirmLabel="Authorize and run"
      busy={busy}
      onConfirm={confirm}
    >
      <div className="space-y-3">
        <dl className="divide-y rounded-md border text-sm">
          {[
            ['Incident', <span key="i" className="font-mono text-xs">{incident.id}</span>],
            ['Action', novelActionLabel(proposal.action_type)],
            ['Target', `${targetLine}${target.container ? `, container ${target.container}` : ''}`],
            ...(change ? [['Change', `${change.before ?? '—'} → ${change.after ?? '—'}`]] : []),
            ['Scope', 'This proposal only, used once'],
            ['Permanent policy changed', <Badge key="p" variant="ok">No</Badge>],
          ].map(([label, value]) => (
            <div key={label} className="flex items-center justify-between gap-4 px-3 py-2">
              <dt className="text-xs text-muted-foreground">{label}</dt>
              <dd className="min-w-0 truncate text-right font-mono text-xs">{value ?? '—'}</dd>
            </div>
          ))}
        </dl>
        <p className="flex items-start gap-2 text-xs text-muted-foreground">
          <ShieldCheck aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
          Every existing safeguard still applies — namespace and deployment allow-lists, protected
          workloads, the per-incident action limit, and this proposal's own confidence floor (which,
          unlike the known-action path, cannot be waived). Sentinel validates recovery afterwards.
        </p>
        {error && <Callout tone="bad">{error}</Callout>}
      </div>
    </ConfirmDialog>
  )
}
