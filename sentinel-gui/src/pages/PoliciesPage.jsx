import { useEffect, useState } from 'react'
import AlertBanner from '../components/AlertBanner.jsx'
import Spinner from '../components/Spinner.jsx'
import { applyPolicyChange, getPolicyConfig, previewPolicyChange } from '../api/config.js'
import { extractErrorMessage } from '../api/client.js'
import { formatRelativeTime, titleCase } from '../utils/format.js'

const CONFIDENCE_FIELDS = [
  { field: 'confidence_restart', label: 'Restart' },
  { field: 'confidence_rollback', label: 'Rollback' },
  { field: 'confidence_scale', label: 'Scale' },
  { field: 'confidence_chaos_reset', label: 'Chaos reset' },
]

const LIMIT_FIELDS = [
  { field: 'min_replicas', label: 'Minimum replicas' },
  { field: 'max_replicas', label: 'Maximum replicas' },
  { field: 'max_actions_per_incident', label: 'Max actions per incident' },
  { field: 'action_cooldown_seconds', label: 'Action cooldown (seconds)' },
  { field: 'deployment_correlation_window_minutes', label: 'Deploy correlation window (minutes)' },
]

const LIST_FIELDS = [
  { field: 'allowed_namespaces', label: 'Allowed namespaces' },
  { field: 'allowed_deployments', label: 'Allowed deployments' },
]

function listToText(list) {
  return (list ?? []).join(', ')
}

function textToList(text) {
  return text
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
}

function diffChanges(current, draft) {
  const changes = {}
  for (const { field } of [...CONFIDENCE_FIELDS, ...LIMIT_FIELDS]) {
    const draftValue = Number(draft[field])
    if (!Number.isNaN(draftValue) && draftValue !== current[field]) {
      changes[field] = draftValue
    }
  }
  for (const { field } of LIST_FIELDS) {
    const draftList = textToList(draft[field] ?? '')
    const currentList = current[field] ?? []
    const same =
      draftList.length === currentList.length && draftList.every((v, i) => v === [...currentList].sort()[i])
    if (!same) changes[field] = draftList
  }
  return changes
}

export default function PoliciesPage() {
  const [data, setData] = useState(null)
  const [draft, setDraft] = useState(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(null)

  const [preview, setPreview] = useState(null) // {valid, errors, diff}
  const [reason, setReason] = useState('')
  const [reviewing, setReviewing] = useState(false)
  const [applying, setApplying] = useState(false)
  const [actionError, setActionError] = useState(null)
  const [justApplied, setJustApplied] = useState(false)

  function loadConfig() {
    setLoading(true)
    getPolicyConfig()
      .then((body) => {
        setData(body)
        setDraft({
          ...Object.fromEntries(
            [...CONFIDENCE_FIELDS, ...LIMIT_FIELDS].map(({ field }) => [field, body.current[field]])
          ),
          ...Object.fromEntries(LIST_FIELDS.map(({ field }) => [field, listToText(body.current[field])])),
        })
        setLoadError(null)
      })
      .catch((err) => setLoadError(extractErrorMessage(err, 'Could not load policy configuration.')))
      .finally(() => setLoading(false))
  }

  useEffect(loadConfig, [])

  function handleFieldChange(field, value) {
    setDraft((prev) => ({ ...prev, [field]: value }))
    setPreview(null)
    setJustApplied(false)
  }

  async function handleReview() {
    const changes = diffChanges(data.current, draft)
    if (Object.keys(changes).length === 0) return
    setReviewing(true)
    setActionError(null)
    try {
      const result = await previewPolicyChange(changes)
      setPreview({ ...result, changes })
    } catch (err) {
      setActionError(extractErrorMessage(err, 'Could not validate these changes.'))
    } finally {
      setReviewing(false)
    }
  }

  async function handleApply() {
    setApplying(true)
    setActionError(null)
    try {
      await applyPolicyChange(preview.changes, reason)
      setPreview(null)
      setReason('')
      setJustApplied(true)
      loadConfig()
    } catch (err) {
      setActionError(extractErrorMessage(err, 'Could not apply these changes.'))
    } finally {
      setApplying(false)
    }
  }

  function handleCancelReview() {
    setPreview(null)
    setActionError(null)
  }

  if (loading) return <Spinner label="Loading policy configuration…" />
  if (loadError) return <AlertBanner>{loadError}</AlertBanner>
  if (!data || !draft) return null

  return (
    <div className="page">
      <div className="page__header">
        <div>
          <h1>Policies</h1>
          <p className="page__subtitle">
            Backed live by Sentinel's real Policy Engine — a change here takes effect on the very next
            incident evaluation.{' '}
            {data.last_changed_at && (
              <>Last changed {formatRelativeTime(data.last_changed_at)} by {data.last_changed_by}.</>
            )}
          </p>
        </div>
      </div>

      {justApplied && !preview && (
        <AlertBanner tone="success">Configuration applied — Sentinel is using the new values now.</AlertBanner>
      )}
      {actionError && <AlertBanner>{actionError}</AlertBanner>}

      {preview && (
        <section className="card card--escalation">
          <h2 className="card__title">Review changes</h2>
          {preview.errors.length > 0 ? (
            <div>
              {preview.errors.map((err) => (
                <p key={err} className="muted">
                  ✗ {err}
                </p>
              ))}
              <button type="button" className="button button--ghost" onClick={handleCancelReview}>
                Back to editing
              </button>
            </div>
          ) : (
            <>
              <table className="table">
                <thead>
                  <tr>
                    <th>Field</th>
                    <th>Current</th>
                    <th>New</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.diff.map((d) => (
                    <tr key={d.field}>
                      <td>{titleCase(d.field)}</td>
                      <td className="mono">{Array.isArray(d.old_value) ? d.old_value.join(', ') || '—' : String(d.old_value)}</td>
                      <td className="mono">{Array.isArray(d.new_value) ? d.new_value.join(', ') || '—' : String(d.new_value)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {preview.diff.filter((d) => d.warning).map((d) => (
                <AlertBanner key={d.field} tone="warn">
                  {d.warning}
                </AlertBanner>
              ))}
              <label className="field">
                <span>Reason (optional, but recommended for the audit trail)</span>
                <textarea
                  className="select feedback-form__note"
                  rows={2}
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                />
              </label>
              <div className="authorization-confirm__actions">
                <button type="button" className="button button--ghost" onClick={handleCancelReview}>
                  Cancel
                </button>
                <button type="button" className="button button--primary" onClick={handleApply} disabled={applying}>
                  {applying ? 'Applying…' : 'Confirm & Apply'}
                </button>
              </div>
            </>
          )}
        </section>
      )}

      {!preview && (
        <>
          <section className="card">
            <h2 className="card__title">Confidence thresholds</h2>
            <p className="muted small">Minimum diagnosis confidence required before Sentinel acts autonomously.</p>
            <div className="grid grid--2">
              {CONFIDENCE_FIELDS.map(({ field, label }) => (
                <label className="field" key={field}>
                  <span>{label}</span>
                  <input
                    type="number"
                    step="0.01"
                    min={data.bounds[field].min}
                    max={data.bounds[field].max}
                    className="select"
                    value={draft[field]}
                    onChange={(e) => handleFieldChange(field, e.target.value)}
                  />
                </label>
              ))}
            </div>
          </section>

          <section className="card">
            <h2 className="card__title">Limits &amp; cooldowns</h2>
            <div className="grid grid--2">
              {LIMIT_FIELDS.map(({ field, label }) => (
                <label className="field" key={field}>
                  <span>{label}</span>
                  <input
                    type="number"
                    min={data.bounds[field].min}
                    max={data.bounds[field].max}
                    className="select"
                    value={draft[field]}
                    onChange={(e) => handleFieldChange(field, e.target.value)}
                  />
                </label>
              ))}
            </div>
          </section>

          <section className="card">
            <h2 className="card__title">Allow-lists</h2>
            <p className="muted small">Comma-separated. An entry here can never include anything on the protected list below.</p>
            {LIST_FIELDS.map(({ field, label }) => (
              <label className="field" key={field}>
                <span>{label}</span>
                <input
                  type="text"
                  className="select"
                  value={draft[field]}
                  onChange={(e) => handleFieldChange(field, e.target.value)}
                />
              </label>
            ))}

            <h3>Protected — never editable, here or anywhere else</h3>
            <div className="grid grid--2">
              <div>
                <span className="muted small">Denied deployments</span>
                <p className="mono">{data.protected.denied_deployments.join(', ') || '—'}</p>
              </div>
              <div>
                <span className="muted small">Denied namespaces</span>
                <p className="mono">{data.protected.denied_namespaces.join(', ') || '—'}</p>
              </div>
            </div>
          </section>

          <button
            type="button"
            className="button button--primary"
            onClick={handleReview}
            disabled={reviewing || Object.keys(diffChanges(data.current, draft)).length === 0}
          >
            {reviewing ? 'Validating…' : 'Review changes'}
          </button>
        </>
      )}
    </div>
  )
}
