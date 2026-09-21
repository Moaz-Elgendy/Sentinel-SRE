import { ShieldCheck } from 'lucide-react'
import { useState } from 'react'
import { authorizeIncident } from '@/api/authorizations'
import { extractErrorMessage } from '@/api/client'
import { ConfirmDialog } from '@/components/sentinel/ConfirmDialog'
import { Callout } from '@/components/sentinel/States'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { notify } from '@/lib/notify'
import { actionLabel } from '@/utils/incident'

/**
 * Grants ONE action on ONE incident. The wording matches what the backend
 * actually does (routers/authorizations.py): the grant is scoped to
 * (incident, action), single-use and short-lived, waives only the confidence
 * check, and never changes the permanent policy.
 */
export function AuthorizeDialog({ incident, action, onOpenChange, onAuthorized }) {
  const [replicas, setReplicas] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const open = Boolean(action)

  async function confirm() {
    setBusy(true)
    setError(null)
    try {
      await authorizeIncident(incident.id, {
        action,
        replicas: action === 'scale_deployment' && replicas !== '' ? Number(replicas) : null,
      })
      notify.success(`${actionLabel(action)} authorized`, { description: 'Sentinel is executing it now. Follow progress on this page.' })
      onOpenChange(null)
      setReplicas('')
      onAuthorized?.()
    } catch (err) {
      setError(extractErrorMessage(err, 'Could not submit the authorization. Nothing was changed.'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          setError(null)
          onOpenChange(null)
        }
      }}
      title={`Authorize: ${actionLabel(action).toLowerCase()}`}
      description="You are granting Sentinel permission to run this one action on this one incident."
      confirmLabel="Authorize and run"
      busy={busy}
      onConfirm={confirm}
    >
      <div className="space-y-3">
        <dl className="divide-y rounded-md border text-sm">
          {[
            ['Incident', <span key="i" className="font-mono text-xs">{incident.id}</span>],
            ['Action', actionLabel(action)],
            ['Scope', 'This incident only, used once'],
            ['Permanent policy changed', <Badge key="p" variant="ok">No</Badge>],
          ].map(([label, value]) => (
            <div key={label} className="flex items-center justify-between gap-4 px-3 py-2">
              <dt className="text-xs text-muted-foreground">{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
        <p className="flex items-start gap-2 text-xs text-muted-foreground">
          <ShieldCheck aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
          This waives only the confidence check. Namespace and deployment allow-lists, protected workloads, cooldown and the action limit still apply, and Sentinel validates recovery afterwards.
        </p>
        {action === 'scale_deployment' && (
          <div className="space-y-1.5">
            <Label htmlFor="replicas">Target replicas (optional)</Label>
            <Input id="replicas" type="number" min="0" value={replicas} onChange={(e) => setReplicas(e.target.value)} placeholder="Leave blank to let Sentinel decide" className="max-w-64" />
          </div>
        )}
        {error && <Callout tone="bad">{error}</Callout>}
      </div>
    </ConfirmDialog>
  )
}
