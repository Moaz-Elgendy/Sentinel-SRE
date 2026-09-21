import { Ban, Check, CircleCheck, CircleX, LoaderCircle, Minus, ShieldCheck, ShieldX } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Timestamp } from '@/components/sentinel/Timestamp'
import { EmptyState } from '@/components/sentinel/States'
import { TONE_TEXT } from '@/components/sentinel/tone'
import { cn } from '@/lib/utils'
import { formatSpan } from '@/utils/format'
import { actionLabel, isActiveIncident, isAwaitingHuman, isHumanAuthorized } from '@/utils/incident'
import { denialReasonLabel, policyCheckLabel, VALIDATION_OUTCOME } from '@/utils/labels'
import { CaseStep } from './CaseStep.jsx'

function targetOf(params) {
  if (!params) return null
  const name = params.deployment ?? params.service
  if (!name) return null
  const extras = [params.replicas != null && `${params.replicas} replicas`, params.target_revision != null && `revision ${params.target_revision}`].filter(Boolean)
  return [`${name}${params.namespace ? ` in ${params.namespace}` : ''}`, ...extras].join(', ')
}

// Each attempt is a four-part chain. state ∈ done | active | blocked | failed | skipped
function chainOf(attempt, live) {
  const { verdict, result, validation } = attempt
  const allowed = verdict?.allowed !== false
  const executed = Boolean(result)
  const succeeded = result?.succeeded !== false
  const validated = validation ? validation.outcome === 'passed' : null
  return [
    { key: 'plan', label: 'Plan', state: 'done', note: actionLabel(attempt.plan?.action) },
    allowed
      ? { key: 'policy', label: 'Policy', state: 'done', note: isHumanAuthorized(attempt) ? 'Approved with SRE waiver' : 'Approved' }
      : { key: 'policy', label: 'Policy', state: 'blocked', note: verdict?.reason ? denialReasonLabel(verdict.reason) : 'Denied' },
    !allowed
      ? { key: 'action', label: 'Action', state: 'skipped', note: 'Not run' }
      : executed
        ? { key: 'action', label: 'Action', state: succeeded ? 'done' : 'failed', note: result.dry_run ? 'Dry run, nothing changed' : succeeded ? 'Succeeded' : 'Failed' }
        : { key: 'action', label: 'Action', state: live ? 'active' : 'skipped', note: live ? 'Running' : 'Not run' },
    !allowed || !executed || !succeeded
      ? { key: 'validation', label: 'Validation', state: 'skipped', note: 'Not run' }
      : validation
        ? { key: 'validation', label: 'Validation', state: validated ? 'done' : 'failed', note: VALIDATION_OUTCOME[validation.outcome] ?? validation.outcome }
        : { key: 'validation', label: 'Validation', state: live ? 'active' : 'skipped', note: live ? 'Checking recovery' : 'Not run' },
  ]
}

const CHAIN_ICON = { done: Check, active: LoaderCircle, blocked: Ban, failed: CircleX, skipped: Minus }
const CHAIN_TONE = { done: '', active: TONE_TEXT.info, blocked: TONE_TEXT.warn, failed: TONE_TEXT.bad, skipped: 'text-muted-foreground' }

function Chain({ steps }) {
  return (
    <ol className="grid grid-cols-2 gap-px overflow-hidden rounded-md border bg-border sm:grid-cols-4">
      {steps.map((step) => {
        const Icon = CHAIN_ICON[step.state]
        return (
          <li key={step.key} className="bg-card px-3 py-2">
            <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Icon aria-hidden="true" className={cn('size-3', CHAIN_TONE[step.state], step.state === 'active' && 'motion-safe:animate-spin')} />
              {step.label}
            </p>
            <p className={cn('mt-0.5 truncate text-sm', step.state === 'skipped' ? 'text-muted-foreground' : 'font-medium', step.state === 'blocked' && TONE_TEXT.warn, step.state === 'failed' && TONE_TEXT.bad)} title={step.note}>
              {step.note}
            </p>
          </li>
        )
      })}
    </ol>
  )
}

function ChecksList({ checks }) {
  const entries = Object.entries(checks ?? {})
  if (entries.length === 0) return null
  return (
    <ul className="space-y-1">
      {entries.map(([key, value]) => {
        const ok = typeof value === 'object' ? value.ok : value
        const detail = typeof value === 'object' ? value.detail : null
        return (
          <li key={key} className="flex items-start gap-2 text-sm">
            {ok ? <CircleCheck aria-label="Passed" className={cn('mt-0.5 size-3.5 shrink-0', TONE_TEXT.ok)} /> : <CircleX aria-label="Failed" className={cn('mt-0.5 size-3.5 shrink-0', TONE_TEXT.bad)} />}
            <span className={cn(!ok && 'font-medium')}>
              {policyCheckLabel(key)}
              {detail && <span className="block text-xs font-normal text-muted-foreground">{detail}</span>}
            </span>
          </li>
        )
      })}
    </ul>
  )
}

function AttemptCard({ attempt, index, total, live }) {
  const { plan, verdict, result, validation } = attempt
  const target = targetOf(plan?.params)
  const human = isHumanAuthorized(attempt)
  const steps = chainOf(attempt, live)
  return (
    <article aria-label={`Attempt ${index + 1}`} className="space-y-3 rounded-lg border p-3.5">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <h3 className="text-sm font-semibold">
          {total > 1 ? `Attempt ${index + 1}: ` : ''}
          {actionLabel(plan?.action)}
        </h3>
        {human && <Badge variant="info">SRE authorized</Badge>}
        {result?.dry_run && <Badge variant="warn">Dry run</Badge>}
        <span className="ml-auto text-xs text-muted-foreground">
          <Timestamp value={attempt.at} />
        </span>
      </header>

      <Chain steps={steps} />

      <div className="grid gap-x-8 gap-y-4 lg:grid-cols-2">
        <div className="space-y-3">
          <div>
            <h4 className="text-xs font-medium text-muted-foreground">Why this action</h4>
            <p className="mt-1 text-sm">{plan?.rationale || 'No rationale recorded.'}</p>
            <p className="tnum mt-1 text-xs text-muted-foreground">
              {target && <>Target: {target}. </>}Confidence {Math.round((plan?.confidence ?? 0) * 100)}%
            </p>
          </div>
          <div>
            <h4 className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
              {verdict?.allowed === false ? <ShieldX aria-hidden="true" className={cn('size-3.5', TONE_TEXT.warn)} /> : <ShieldCheck aria-hidden="true" className="size-3.5" />}
              Policy checks
            </h4>
            {verdict?.detail && <p className="mt-1 text-xs text-muted-foreground">{verdict.detail}</p>}
            <div className="mt-1.5">
              <ChecksList checks={verdict?.checks} />
            </div>
          </div>
        </div>

        <div className="space-y-3">
          {result && (
            <div>
              <h4 className="text-xs font-medium text-muted-foreground">What happened</h4>
              <p className="mt-1 text-sm">{result.detail || (result.succeeded ? 'Completed.' : 'Did not complete.')}</p>
              {result.duration_seconds != null && <p className="tnum mt-0.5 text-xs text-muted-foreground">Took {formatSpan(result.duration_seconds)}</p>}
            </div>
          )}
          {validation && (
            <div>
              <h4 className="text-xs font-medium text-muted-foreground">Did it recover?</h4>
              <p className={cn('mt-1 text-sm font-medium', validation.outcome === 'passed' ? TONE_TEXT.ok : TONE_TEXT.bad)}>{VALIDATION_OUTCOME[validation.outcome] ?? validation.outcome}</p>
              {validation.detail && <p className="text-sm text-muted-foreground">{validation.detail}</p>}
              <div className="mt-1.5">
                <ChecksList checks={validation.checks} />
              </div>
            </div>
          )}
          {!result && verdict?.allowed === false && <p className="text-sm text-muted-foreground">Policy refused this action, so nothing was changed in the cluster.</p>}
        </div>
      </div>
    </article>
  )
}

/** Step 3: what Sentinel chose to do, why policy did or didn't allow it, and what actually happened. */
export function DecisionSection({ incident }) {
  const attempts = incident.attempts ?? []
  const live = isActiveIncident(incident)
  const last = attempts[attempts.length - 1]
  const validating = incident.status === 'validating'

  return (
    <CaseStep step="3" title="What it decided and did" description="Each action considered: the plan, the policy verdict, the result and whether it recovered">
      {attempts.length === 0 ? (
        <EmptyState
          compact
          title={isAwaitingHuman(incident) ? 'Sentinel did not act' : live ? 'No action decided yet' : 'No action was attempted'}
          description={
            isAwaitingHuman(incident)
              ? 'It found no safe autonomous action for this diagnosis. See what it considered above, and decide how to proceed.'
              : live
                ? 'A remediation decision appears here once Sentinel has weighed its options.'
                : 'This incident ended without a remediation attempt.'
          }
        />
      ) : (
        <div className="space-y-3">
          {attempts.map((attempt, i) => (
            <AttemptCard key={`${attempt.at}-${i}`} attempt={attempt} index={i} total={attempts.length} live={live && attempt === last} />
          ))}
          {validating && <p className="text-xs text-muted-foreground">If recovery isn’t confirmed, Sentinel re-investigates and tries the next safe action, or escalates to you.</p>}
        </div>
      )}
    </CaseStep>
  )
}
