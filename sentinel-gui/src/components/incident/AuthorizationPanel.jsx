import { useEffect, useState } from 'react'
import { authorizeIncident, listAuthorizations } from '../../api/authorizations.js'
import { formatTimestamp, titleCase } from '../../utils/format.js'

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
    listAuthorizations(incident.id).then(setHistory).catch(() => setHistory([]))
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
      setError('Could not submit the authorization. Please try again.')
    } finally {
      setSubmitting(false)
    }
  }

  const remediationActions = actionTypes.filter((a) => a !== 'escalate')

  return (
    <div className="authorization-panel">
      <p className="authorization-panel__why">
        <strong>Why was this escalated?</strong> {incident.escalation_detail || 'No further detail recorded.'}
      </p>

      {history.length > 0 && (
        <div className="authorization-history">
          {history.map((row) => (
            <div key={row.id} className="authorization-history__row">
              <span className="mono">{titleCase(row.action)}</span>
              <span className={row.consumed_at ? 'tag' : 'tag tag--muted'}>
                {row.consumed_at ? titleCase(row.consumed_result) : 'granted, not yet used'}
              </span>
              <span className="muted small">{formatTimestamp(row.granted_at)}</span>
            </div>
          ))}
        </div>
      )}

      {justSubmitted ? (
        <p className="muted">Authorization submitted — Sentinel is executing it now. Watch the flow above.</p>
      ) : pendingAction ? (
        <div className="authorization-confirm">
          <p className="authorization-confirm__title">Grant permission for this incident only</p>
          <dl className="fact-list">
            <div className="fact-row">
              <dt>Incident</dt>
              <dd className="mono">{incident.id}</dd>
            </div>
            <div className="fact-row">
              <dt>Action</dt>
              <dd className="mono">{pendingAction}</dd>
            </div>
            <div className="fact-row">
              <dt>Scope</dt>
              <dd>this incident only</dd>
            </div>
            <div className="fact-row">
              <dt>Permanent policy changed</dt>
              <dd>NO</dd>
            </div>
          </dl>
          {pendingAction === 'scale_deployment' && (
            <label className="field">
              <span>Target replicas (optional — leave blank to let Sentinel decide)</span>
              <input
                type="number"
                min="0"
                className="select"
                value={replicas}
                onChange={(e) => setReplicas(e.target.value)}
              />
            </label>
          )}
          {error && <p className="muted">{error}</p>}
          <div className="authorization-confirm__actions">
            <button type="button" className="button button--ghost" onClick={() => setPendingAction(null)}>
              Cancel
            </button>
            <button type="button" className="button button--primary" onClick={handleConfirm} disabled={submitting}>
              {submitting ? 'Granting…' : 'Confirm authorization'}
            </button>
          </div>
        </div>
      ) : (
        <div className="authorization-panel__actions">
          <span className="muted small">Recommended actions:</span>
          {remediationActions.map((action) => (
            <button
              key={action}
              type="button"
              className="button button--ghost"
              onClick={() => setPendingAction(action)}
            >
              {titleCase(action)}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
