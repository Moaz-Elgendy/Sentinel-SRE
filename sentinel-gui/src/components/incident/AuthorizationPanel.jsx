import { useEffect, useState } from 'react'
import { authorizeIncident, listAuthorizations } from '../../api/authorizations.js'
import { formatTimestamp, titleCase } from '../../utils/format.js'
import AlertBanner from '../ui/AlertBanner.jsx'
import Button from '../ui/Button.jsx'
import Field from '../ui/Field.jsx'
import Tag from '../ui/Tag.jsx'

/**
 * Only rendered when an incident is currently escalated (see
 * IncidentDetailPage). Lets an SRE grant a scoped, one-time exception for
 * ONE action on THIS incident — see sentinel-ai/app/routers/authorizations.py
 * and lifecycle/policy.py's `human_override` docstring for what this
 * actually changes on the backend (exactly the confidence check; nothing
 * else). This panel never claims more than that: the confirmation step
 * below states the scope explicitly, matching the backend's own audit
 * fields (`scope`, `permanent_policy_changed`) rather than softer wording.
 */
export default function AuthorizationPanel({ incident, actionTypes, onAuthorized }) {
  const [history, setHistory] = useState([])
  const [pendingAction, setPendingAction] = useState(null) // action awaiting confirmation
  const [replicas, setReplicas] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)
  const [justSubmitted, setJustSubmitted] = useState(false)

  const refreshHistory = () => {
    listAuthorizations(incident.id)
      .then(setHistory)
      .catch(() => setHistory([]))
  }

  useEffect(() => {
    refreshHistory()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [incident.id])

  async function handleConfirm() {
    setSubmitting(true)
    setError(null)
    try {
      await authorizeIncident(incident.id, {
        action: pendingAction,
        replicas: pendingAction === 'scale_deployment' && replicas !== '' ? Number(replicas) : null,
      })
      setJustSubmitted(true)
      setPendingAction(null)
      refreshHistory()
      onAuthorized?.()
    } catch {
      setError('Could not submit the authorization. Nothing was changed — please try again.')
    } finally {
      setSubmitting(false)
    }
  }

  const remediationActions = actionTypes.filter((a) => a !== 'escalate')

  return (
    <div className="stack">
      <div>
        <div className="eyebrow">Why was this escalated?</div>
        <p className="authorization__why">{incident.escalation_detail || 'No further detail recorded.'}</p>
      </div>

      {history.length > 0 && (
        <div>
          <div className="eyebrow authorization__history-title">Previous authorizations</div>
          <ul className="authorization__history">
            {history.map((row) => (
              <li key={row.id}>
                <span className="mono">{titleCase(row.action)}</span>
                <Tag tone={row.consumed_at ? 'info' : 'neutral'}>
                  {row.consumed_at ? titleCase(row.consumed_result) : 'Granted, not yet used'}
                </Tag>
                <span className="muted small">{formatTimestamp(row.granted_at)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {justSubmitted ? (
        <AlertBanner tone="success" title="Authorization submitted">
          Sentinel is executing it now — follow progress in the operations panel.
        </AlertBanner>
      ) : pendingAction ? (
        <div className="confirm-box">
          <p className="confirm-box__title">Grant permission for this incident only</p>
          <dl>
            <div className="dl__row">
              <dt>Incident</dt>
              <dd className="mono">{incident.id}</dd>
            </div>
            <div className="dl__row">
              <dt>Action</dt>
              <dd className="mono">{pendingAction}</dd>
            </div>
            <div className="dl__row">
              <dt>Scope</dt>
              <dd>This incident only</dd>
            </div>
            <div className="dl__row">
              <dt>Permanent policy changed</dt>
              <dd>
                <Tag tone="ok">No</Tag>
              </dd>
            </div>
          </dl>
          {pendingAction === 'scale_deployment' && (
            <Field label="Target replicas" hint="Optional — leave blank to let Sentinel decide.">
              <input
                type="number"
                min="0"
                className="input"
                value={replicas}
                onChange={(e) => setReplicas(e.target.value)}
              />
            </Field>
          )}
          {error && <AlertBanner>{error}</AlertBanner>}
          <div className="confirm-box__actions">
            <Button variant="ghost" onClick={() => setPendingAction(null)} disabled={submitting}>
              Cancel
            </Button>
            <Button variant="primary" onClick={handleConfirm} busy={submitting} busyLabel="Granting…">
              Confirm authorization
            </Button>
          </div>
        </div>
      ) : (
        <div>
          <div className="eyebrow authorization__history-title">Authorize one action</div>
          <div className="cluster">
            {remediationActions.map((action) => (
              <Button key={action} onClick={() => setPendingAction(action)}>
                {titleCase(action)}
              </Button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
