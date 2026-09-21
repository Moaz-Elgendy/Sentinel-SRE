import { Hand, RotateCcw, Scale, Undo2, Wrench, FlaskConical, Rocket } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { listActionTypes } from '@/api/meta'
import { listAuthorizations } from '@/api/authorizations'
import { extractErrorMessage } from '@/api/client'
import { reinvestigateIncident } from '@/api/incidents'
import { ConfirmDialog } from '@/components/sentinel/ConfirmDialog'
import { Callout } from '@/components/sentinel/States'
import { Timestamp } from '@/components/sentinel/Timestamp'
import { TONE_SURFACE, TONE_TEXT } from '@/components/sentinel/tone'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { notify } from '@/lib/notify'
import { cn } from '@/lib/utils'
import { actionLabel } from '@/utils/incident'
import { denialReasonLabel, escalationReasonLabel } from '@/utils/labels'
import { sentenceCase } from '@/utils/format'
import { AuthorizeDialog } from './AuthorizeDialog.jsx'

const ACTION_ICON = {
  restart_deployment: RotateCcw,
  rollback_deployment: Undo2,
  scale_deployment: Scale,
  reset_chaos_fault: FlaskConical,
}
const FALLBACK_ACTIONS = ['restart_deployment', 'rollback_deployment', 'scale_deployment', 'reset_chaos_fault']

/**
 * Shown only while an incident is waiting on a human. It answers, in order:
 * why did Sentinel stop, what did it consider and why was that refused, and
 * what can I do about it — including the two ways forward (authorize one
 * action, or ask Sentinel to look again).
 */
export function AttentionPanel({ incident, onChanged }) {
  const record = incident.escalation_record
  const rejected = record?.rejected_actions ?? []
  const [actions, setActions] = useState(FALLBACK_ACTIONS)
  const [history, setHistory] = useState([])
  const [pendingAction, setPendingAction] = useState(null)
  const [reinvestigateOpen, setReinvestigateOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [reinvestigateError, setReinvestigateError] = useState(null)

  useEffect(() => {
    listActionTypes()
      .then((all) => setActions(all.filter((a) => a !== 'escalate')))
      .catch(() => {})
  }, [])

  const refreshHistory = useCallback(() => {
    listAuthorizations(incident.id)
      .then(setHistory)
      .catch(() => setHistory([]))
  }, [incident.id])

  useEffect(() => {
    refreshHistory()
  }, [refreshHistory])

  async function reinvestigate() {
    setBusy(true)
    setReinvestigateError(null)
    try {
      await reinvestigateIncident(incident.id)
      notify.success('Re-investigation started', { description: 'Sentinel is gathering fresh evidence.' })
      setReinvestigateOpen(false)
      onChanged?.()
    } catch (err) {
      setReinvestigateError(extractErrorMessage(err, 'Could not start a re-investigation.'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section aria-labelledby="attention-heading" className={cn('overflow-hidden rounded-lg border', TONE_SURFACE.warn)}>
      <header className="flex items-start gap-3 border-b border-warn-edge px-4 py-3">
        <Hand aria-hidden="true" className={cn('mt-0.5 size-4.5 shrink-0', TONE_TEXT.warn)} />
        <div className="min-w-0 flex-1">
          <h2 id="attention-heading" className="text-sm font-semibold">
            Sentinel is waiting for your decision
          </h2>
          <p className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
            {incident.escalation_reason && <Badge variant="warn">{escalationReasonLabel(incident.escalation_reason)}</Badge>}
            {record?.at && (
              <span>
                Escalated <Timestamp value={record.at} />
              </span>
            )}
          </p>
        </div>
      </header>

      <div className="grid gap-x-8 gap-y-4 px-4 py-3.5 lg:grid-cols-2">
        <div className="space-y-3">
          <div>
            <h3 className="text-xs font-medium text-muted-foreground">Why it stopped</h3>
            <p className="mt-1 text-sm">{incident.escalation_detail || 'No further detail was recorded.'}</p>
          </div>
          <div>
            <h3 className="text-xs font-medium text-muted-foreground">What it considered</h3>
            {rejected.length === 0 ? (
              <p className="mt-1 text-sm text-muted-foreground">Sentinel had no candidate action for this diagnosis, so there was nothing to approve or deny.</p>
            ) : (
              <ul className="mt-1.5 divide-y rounded-md border border-warn-edge bg-background/60">
                {rejected.map((r, i) => (
                  <li key={`${r.action}-${i}`} className="flex flex-wrap items-center justify-between gap-x-4 gap-y-0.5 px-3 py-2 text-sm">
                    <span className="font-medium">{actionLabel(r.action)}</span>
                    <span className="text-xs text-muted-foreground">
                      {r.denial_reason ? denialReasonLabel(r.denial_reason) : 'Not approved'}
                      {r.confidence != null && r.required_confidence != null && (
                        <span className="tnum ml-2">
                          {Math.round(r.confidence * 100)}% vs {Math.round(r.required_confidence * 100)}% required
                        </span>
                      )}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>

        <div className="space-y-3">
          <div>
            <h3 className="text-xs font-medium text-muted-foreground">Authorize one action for this incident</h3>
            <div className="mt-1.5 flex flex-wrap gap-2">
              {actions.map((action) => {
                const Icon = ACTION_ICON[action] ?? Wrench
                return (
                  <Button key={action} variant="outline" size="sm" onClick={() => setPendingAction(action)} className="bg-background/60">
                    <Icon /> {actionLabel(action)}
                  </Button>
                )
              })}
            </div>
            <p className="mt-1.5 text-xs text-muted-foreground">Single-use and limited to this incident. Policy checks other than confidence still apply.</p>
          </div>
          <div>
            <h3 className="text-xs font-medium text-muted-foreground">Or ask Sentinel to look again</h3>
            <Button variant="outline" size="sm" className="mt-1.5 bg-background/60" onClick={() => setReinvestigateOpen(true)}>
              <Rocket /> Re-investigate
            </Button>
          </div>
        </div>
      </div>

      {history.length > 0 && (
        <div className="border-t border-warn-edge px-4 py-3">
          <h3 className="text-xs font-medium text-muted-foreground">Previous authorizations on this incident</h3>
          <ul className="mt-1.5 space-y-1 text-sm">
            {history.map((row) => (
              <li key={row.id} className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <span>{actionLabel(row.action)}</span>
                <Badge variant={row.consumed_at ? 'info' : 'neutral'}>{row.consumed_at ? sentenceCase(row.consumed_result) : 'Granted, not yet used'}</Badge>
                <span className="text-xs text-muted-foreground">
                  <Timestamp value={row.granted_at} />
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <AuthorizeDialog incident={incident} action={pendingAction} onOpenChange={setPendingAction} onAuthorized={() => { refreshHistory(); onChanged?.() }} />

      <ConfirmDialog
        open={reinvestigateOpen}
        onOpenChange={setReinvestigateOpen}
        title="Re-investigate this incident?"
        description="Sentinel will collect fresh evidence and run its diagnosis and policy checks again. It may act on its own if the new picture is clear enough."
        confirmLabel="Re-investigate"
        busy={busy}
        onConfirm={reinvestigate}
      >
        {reinvestigateError && <Callout tone="bad">{reinvestigateError}</Callout>}
      </ConfirmDialog>
    </section>
  )
}
