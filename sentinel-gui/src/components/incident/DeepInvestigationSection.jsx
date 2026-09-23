import { Ban, CheckCheck, CircleX, LoaderCircle, Sparkles, Wand2 } from 'lucide-react'
import { useState } from 'react'
import { extractErrorMessage } from '@/api/client'
import { rejectDeepProposal, suggestFix } from '@/api/authorizations'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { ConfirmDialog } from '@/components/sentinel/ConfirmDialog'
import { Callout } from '@/components/sentinel/States'
import { Timestamp } from '@/components/sentinel/Timestamp'
import { TONE_TEXT } from '@/components/sentinel/tone'
import { cn } from '@/lib/utils'
import { notify } from '@/lib/notify'
import { describeNovelActionTarget, deepProposalStatusLabel, novelActionLabel, riskLevelLabel, RISK_LEVEL_TONE } from '@/utils/labels'
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

// lifecycle/deep_investigation.py's DeepInvestigationTrace.outcome values —
// "running" is set at trace creation and never persisted mid-flight (the
// GUI's "in progress" state comes from incident.deep_investigation_running,
// a separate boolean, not from reading a half-written trace).
const TRACE_OUTCOME_LABEL = {
  proposal_ready: 'produced a proposal',
  no_safe_fix: 'found no safe fix',
  rejected_by_policy: 'produced a proposal policy rejected',
}

function ProposalCard({ proposal, onAuthorize, onReject, rejecting }) {
  const target = proposal.target ?? {}
  const Icon = STATUS_ICON[proposal.status] ?? Sparkles
  const spin = proposal.status === 'authorized' || proposal.status === 'executing'
  const change = describeNovelActionTarget(proposal.action_type, target)

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
            {change && (change.before != null || change.after != null) && (
              <p className="tnum mt-1 text-xs text-muted-foreground">
                {change.label && <code className="font-mono">{change.label}</code>}{change.label && ': '}
                {change.before != null && <code className="font-mono">{String(change.before)}</code>}
                {' → '}
                <code className="font-mono">{String(change.after ?? '—')}</code>
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
          <div className="ml-auto flex gap-2">
            <Button size="sm" variant="outline" onClick={() => onReject(proposal)} disabled={rejecting}>
              {rejecting && <LoaderCircle className="animate-spin" />}
              Reject
            </Button>
            <Button size="sm" onClick={() => onAuthorize(proposal)}>
              Authorize
            </Button>
          </div>
        )}
      </div>
    </article>
  )
}

/** One line per completed iteration of the bounded read-only tool-use loop
 * (lifecycle/deep_investigation.py's investigate_deep / InvestigationIteration)
 * — the audit trail for exactly what Sentinel looked at before it proposed
 * (or declined to propose) anything. */
function TraceIterations({ trace }) {
  const iterations = trace.iterations ?? []
  if (iterations.length === 0) return null
  return (
    <ol className="mt-2 space-y-1.5 border-t pt-2 text-xs text-muted-foreground">
      {iterations.map((it) => {
        const call = it.tool_call
        return (
          <li key={it.iteration} className="flex flex-wrap items-baseline gap-x-1.5">
            <span className="tnum shrink-0 font-medium text-foreground">#{it.iteration}</span>
            {it.hypothesis && <span>{it.hypothesis}</span>}
            {call && (
              <span className="flex items-center gap-1">
                <code className="rounded bg-muted px-1 font-mono">{call.tool}</code>
                <span className={call.succeeded ? '' : TONE_TEXT.bad}>
                  {call.succeeded ? call.result_summary : call.error || 'failed'}
                </span>
              </span>
            )}
          </li>
        )
      })}
    </ol>
  )
}

/** Summarizes the most recent Deep Investigation run — real backend state
 * only (DeepInvestigationTrace), never a guess: which trigger started it,
 * how many LLM turns and tool calls it actually spent, and what it
 * concluded. Shown for both outcomes (a proposal, or a bounded, honest
 * "no_safe_fix") so "Sentinel looked and found nothing" is as visible as
 * "Sentinel looked and proposed something". */
function TraceSummary({ trace }) {
  const outcomeLabel = TRACE_OUTCOME_LABEL[trace.outcome] ?? trace.outcome
  return (
    <div className="rounded-lg border p-3.5 text-sm">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <Badge variant={trace.outcome === 'proposal_ready' ? 'info' : 'neutral'}>
          {trace.trigger === 'suggest_fix' ? 'Suggest Fix' : 'Automatic'}
        </Badge>
        <span>Investigation {outcomeLabel}</span>
        <span className="tnum text-xs text-muted-foreground">
          {(trace.iterations ?? []).length} iteration{(trace.iterations ?? []).length === 1 ? '' : 's'}, {trace.tool_call_count ?? 0} tool call{trace.tool_call_count === 1 ? '' : 's'}
        </span>
        <span className="ml-auto text-xs text-muted-foreground">
          <Timestamp value={trace.started_at} />
        </span>
      </div>
      {trace.reason && <p className="mt-1.5 text-xs text-muted-foreground">{trace.reason}</p>}
      <TraceIterations trace={trace} />
    </div>
  )
}

/**
 * Deep Investigation: a bounded, evidence-only LLM proposal for a novel typed
 * action (lifecycle/deep_investigation.py). Historically produced only when
 * known remediation was insufficient; now also reachable at any time via the
 * explicit "Suggest Fix" button (orchestrator.suggest_fix), so this section
 * is always shown — not gated on proposals already existing — with an
 * empty-state hint and the Suggest Fix action when there is nothing yet.
 *
 * Never autonomous: every proposal starts (and, until an SRE acts, stays) at
 * `suggested` — see DeepRemediationProposal's own docstring. Authorizing one
 * goes through the exact same human-in-the-loop endpoint as the known-action
 * authorize flow, just for a structured novel action instead of a menu pick;
 * declining one goes through a new, cluster-inert /reject endpoint.
 */
export function DeepInvestigationSection({ incident, onChanged }) {
  const proposals = incident.deep_proposals ?? []
  const traces = incident.deep_investigation_traces ?? []
  const running = Boolean(incident.deep_investigation_running)
  const [pending, setPending] = useState(null)
  const [rejectingId, setRejectingId] = useState(null)
  const [suggestOpen, setSuggestOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [suggestError, setSuggestError] = useState(null)

  const sorted = [...proposals].sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0))
  const latestTrace = traces.length ? traces[traces.length - 1] : null
  const hasOpenProposal = sorted.some((p) => p.status === 'suggested')
  const canInvestigate = Boolean(incident.evidence) && Boolean(incident.hypothesis) && Boolean(incident.target_deployment)

  async function reject(proposal) {
    setRejectingId(proposal.id)
    try {
      await rejectDeepProposal(incident.id, proposal.id)
      onChanged?.()
    } catch (err) {
      notify.error('Could not reject the proposal', { description: extractErrorMessage(err, '') || undefined })
    } finally {
      setRejectingId(null)
    }
  }

  async function suggest() {
    setBusy(true)
    setSuggestError(null)
    try {
      await suggestFix(incident.id)
      notify.success('Deep Investigation started', {
        description: 'Sentinel is reasoning about this incident now. Follow progress on this page.',
      })
      setSuggestOpen(false)
      onChanged?.()
    } catch (err) {
      setSuggestError(extractErrorMessage(err, 'Could not start Deep Investigation.'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <CaseStep
      step="4"
      title="Deep investigation"
      description="A bounded LLM proposal for a novel action — from known remediation being insufficient, or requested directly below — never executed without your authorization"
      actions={
        <Button
          size="sm"
          variant="outline"
          onClick={() => setSuggestOpen(true)}
          disabled={running || !canInvestigate}
        >
          {running ? <LoaderCircle className="animate-spin" /> : <Wand2 />}
          {running ? 'Investigating…' : 'Suggest fix'}
        </Button>
      }
    >
      <div className="space-y-3">
        {running && (
          <Callout tone="info" icon={LoaderCircle}>
            Sentinel is running a bounded, read-only investigation now — reasoning, one tool call at a time, about what
            (if anything) it can safely propose. This page updates automatically.
          </Callout>
        )}
        {!canInvestigate && !running && sorted.length === 0 && (
          <p className="text-xs text-muted-foreground">
            Suggest Fix needs at least one completed investigation (evidence and a diagnosis) to reason from — it isn’t
            available yet for this incident.
          </p>
        )}
        {!running && !hasOpenProposal && sorted.length === 0 && latestTrace && <TraceSummary trace={latestTrace} />}
        {!running && sorted.length === 0 && !latestTrace && canInvestigate && (
          <p className="text-xs text-muted-foreground">
            No Deep Investigation has run for this incident yet. Use Suggest fix to ask Sentinel for a bounded, typed
            remediation proposal right now.
          </p>
        )}
        {!running && hasOpenProposal && (
          <p className="text-xs text-muted-foreground">A proposal below is unauthorized and can still be acted on.</p>
        )}
        {sorted.map((proposal) => (
          <ProposalCard
            key={proposal.id}
            proposal={proposal}
            onAuthorize={setPending}
            onReject={reject}
            rejecting={rejectingId === proposal.id}
          />
        ))}
      </div>

      <AuthorizeDeepProposalDialog
        incident={incident}
        proposal={pending}
        onOpenChange={setPending}
        onAuthorized={() => onChanged?.()}
      />

      <ConfirmDialog
        open={suggestOpen}
        onOpenChange={(next) => {
          if (!next) setSuggestError(null)
          setSuggestOpen(next)
        }}
        title="Ask Sentinel to suggest a fix?"
        description="Sentinel will run a bounded, read-only investigation (a handful of tool calls, capped time) and, if it finds a safe one, propose a single typed action. Nothing executes without your separate authorization afterwards."
        confirmLabel="Suggest fix"
        busy={busy}
        onConfirm={suggest}
      >
        {suggestError && <Callout tone="bad">{suggestError}</Callout>}
      </ConfirmDialog>
    </CaseStep>
  )
}
