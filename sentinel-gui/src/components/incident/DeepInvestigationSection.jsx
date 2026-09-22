import { Ban, CheckCheck, CircleX, LoaderCircle, Sparkles } from 'lucide-react'
import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Timestamp } from '@/components/sentinel/Timestamp'
import { TONE_TEXT } from '@/components/sentinel/tone'
import { cn } from '@/lib/utils'
import { isAwaitingHuman } from '@/utils/incident'
import { deepProposalStatusLabel, novelActionLabel, riskLevelLabel, RISK_LEVEL_TONE } from '@/utils/labels'
import { AuthorizeDeepProposalDialog } from './AuthorizeDeepProposalDialog.jsx'
import { CaseStep } from './CaseStep.jsx'

const STATUS_ICON = {
  suggested: Sparkles,
  authorized: LoaderCircle,
  executing: LoaderCircle,
  executed: CheckCheck,
  validated: CheckCheck,
  failed: CircleX,
  rejected: Ban,
  expired: Ban,
}

const STATUS_TONE = {
  suggested: TONE_TEXT.info,
  authorized: TONE_TEXT.info,
  executing: TONE_TEXT.info,
  executed: TONE_TEXT.ok,
  validated: TONE_TEXT.ok,
  failed: TONE_TEXT.bad,
  rejected: 'text-muted-foreground',
  expired: 'text-muted-foreground',
}

function ProposalCard({ proposal, onAuthorize }) {
  const target = proposal.target ?? {}
  const Icon = STATUS_ICON[proposal.status] ?? Sparkles
  const spin = proposal.status === 'authorized' || proposal.status === 'executing'

  return (
    <article aria-label={`Deep Investigation proposal ${proposal.id}`} className="space-y-3 rounded-lg border p-3.5">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold">
          <Icon aria-hidden="true" className={cn('size-4', STATUS_TONE[proposal.status], spin && 'motion-safe:animate-spin')} />
          {novelActionLabel(proposal.action_type)}
        </h3>
        <Badge variant={RISK_LEVEL_TONE[proposal.risk_level] ?? 'neutral'}>{riskLevelLabel(proposal.risk_level)}</Badge>
        <span className="tnum text-xs text-muted-foreground">{Math.round((proposal.confidence ?? 0) * 100)}% confidence</span>
        <span className="ml-auto text-xs text-muted-foreground">
          <Timestamp value={proposal.created_at} />
        </span>
      </header>

      <div className="grid gap-x-8 gap-y-4 lg:grid-cols-2">
        <div className="space-y-3">
          <div>
            <h4 className="text-xs font-medium text-muted-foreground">Problem</h4>
            <p className="mt-1 text-sm">{proposal.problem}</p>
          </div>
          <div>
            <h4 className="text-xs font-medium text-muted-foreground">Proposed root cause</h4>
            <p className="mt-1 text-sm">{proposal.root_cause}</p>
          </div>
          <div>
            <h4 className="text-xs font-medium text-muted-foreground">Reasoning</h4>
            <p className="mt-1 text-sm">{proposal.reason}</p>
          </div>
        </div>
        <div className="space-y-3">
          <div>
            <h4 className="text-xs font-medium text-muted-foreground">Proposed change</h4>
            <p className="mt-1 font-mono text-xs break-all">{proposal.rendered_command}</p>
            {target.previous_value_existed && (
              <p className="tnum mt-1 text-xs text-muted-foreground">
                Currently {target.previous_value ? <code className="font-mono">{target.previous_value}</code> : 'set'}
                {proposal.action_type === 'set_env_var' && target.value && (
                  <>
                    {' '}→ <code className="font-mono">{target.value}</code>
                  </>
                )}
              </p>
            )}
          </div>
          <div>
            <h4 className="text-xs font-medium text-muted-foreground">Expected effect</h4>
            <p className="mt-1 text-sm">{proposal.expected_effect}</p>
          </div>
          <div>
            <h4 className="text-xs font-medium text-muted-foreground">How Sentinel will validate it</h4>
            <p className="mt-1 text-sm">{proposal.validation_plan}</p>
          </div>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t pt-3">
        <Badge variant={proposal.status === 'suggested' ? 'info' : proposal.status === 'rejected' || proposal.status === 'failed' ? 'bad' : proposal.status === 'validated' || proposal.status === 'executed' ? 'ok' : 'neutral'}>
          {deepProposalStatusLabel(proposal.status)}
        </Badge>
        {proposal.status === 'rejected' && proposal.rejected_reason && <p className="text-xs text-muted-foreground">{proposal.rejected_reason}</p>}
        {proposal.result_detail && proposal.status !== 'rejected' && <p className="text-xs text-muted-foreground">{proposal.result_detail}</p>}
        {proposal.status === 'suggested' && (
          <Button size="sm" className="ml-auto" onClick={() => onAuthorize(proposal)}>
            Authorize
          </Button>
        )}
      </div>
    </article>
  )
}

/**
 * Deep Investigation: a bounded, evidence-only LLM proposal for a novel typed
 * action, produced only when known remediation was insufficient or denied
 * (lifecycle/deep_investigation.py). Deliberately renders nothing when the
 * incident never reached this path — most incidents resolve through the
 * four known actions and never generate a proposal — so this section, unlike
 * the always-present Observed/Diagnosis/Decision steps, only appears once
 * there is something to show.
 *
 * Never autonomous: every proposal starts (and, until an SRE acts, stays) at
 * `suggested` — see DeepRemediationProposal's own docstring. Authorizing one
 * goes through the exact same human-in-the-loop endpoint as the known-action
 * authorize flow, just for a structured novel action instead of a menu pick.
 */
export function DeepInvestigationSection({ incident, onChanged }) {
  const proposals = incident.deep_proposals ?? []
  const [pending, setPending] = useState(null)
  if (proposals.length === 0) return null

  const awaiting = isAwaitingHuman(incident)
  const sorted = [...proposals].sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0))

  return (
    <CaseStep
      step="4"
      title="Deep investigation"
      description="A bounded LLM proposal for a novel action, generated only because known remediation was insufficient — never executed without your authorization"
    >
      <div className="space-y-3">
        {!awaiting && sorted.some((p) => p.status === 'suggested') && (
          <p className="text-xs text-muted-foreground">
            This incident is no longer awaiting a decision, but a proposal below is still unauthorized and can still be acted on.
          </p>
        )}
        {sorted.map((proposal) => (
          <ProposalCard key={proposal.id} proposal={proposal} onAuthorize={setPending} />
        ))}
      </div>

      <AuthorizeDeepProposalDialog
        incident={incident}
        proposal={pending}
        onOpenChange={setPending}
        onAuthorized={() => onChanged?.()}
      />
    </CaseStep>
  )
}
