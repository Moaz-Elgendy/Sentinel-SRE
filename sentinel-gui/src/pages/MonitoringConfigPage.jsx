import { useEffect, useState } from 'react'
import AlertBanner from '../components/AlertBanner.jsx'
import Spinner from '../components/Spinner.jsx'
import { applyMonitoringChange, getMonitoringConfig, previewMonitoringChange } from '../api/config.js'
import { extractErrorMessage } from '../api/client.js'
import { formatRelativeTime, titleCase } from '../utils/format.js'

// Bounds (min/max) come from the backend's /api/config/monitoring `bounds`
// payload — see monitoring_admin.py's TIMEOUT_BOUNDS, single source of truth.
const FIELDS = [
  { field: 'prometheus_url', label: 'Prometheus URL', type: 'string' },
  { field: 'prometheus_timeout_seconds', label: 'Prometheus timeout (seconds)', type: 'float' },
  { field: 'loki_url', label: 'Loki URL', type: 'string' },
  { field: 'loki_timeout_seconds', label: 'Loki timeout (seconds)', type: 'float' },
]

function diffChanges(current, draft) {
  const changes = {}
  for (const { field, type } of FIELDS) {
    const draftValue = draft[field]
    if (type === 'float') {
      const num = Number(draftValue)
      if (!Number.isNaN(num) && num !== current[field]) changes[field] = num
    } else if (draftValue !== current[field]) {
      changes[field] = draftValue
    }
  }
  return changes
}

export default function MonitoringConfigPage() {
  const [data, setData] = useState(null)
  const [draft, setDraft] = useState(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(null)

  const [preview, setPreview] = useState(null)
  const [reason, setReason] = useState('')
  const [reviewing, setReviewing] = useState(false)
  const [applying, setApplying] = useState(false)
  const [actionError, setActionError] = useState(null)
  const [justApplied, setJustApplied] = useState(false)

  function loadConfig() {
    setLoading(true)
    getMonitoringConfig()
      .then((body) => {
        setData(body)
        setDraft(Object.fromEntries(FIELDS.map(({ field }) => [field, body.current[field]])))
        setLoadError(null)
      })
      .catch((err) => setLoadError(extractErrorMessage(err, 'Could not load monitoring configuration.')))
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
      const result = await previewMonitoringChange(changes)
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
      await applyMonitoringChange(preview.changes, reason)
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

  if (loading) return <Spinner label="Loading monitoring configuration…" />
  if (loadError) return <AlertBanner>{loadError}</AlertBanner>
  if (!data || !draft) return null

  const readOnly = data.read_only
  const k8s = readOnly.kubernetes

  return (
    <div className="page">
      <div className="page__header">
        <div>
          <h1>Monitoring</h1>
          <p className="page__subtitle">
            Prometheus and Loki connection settings.{' '}
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

      {preview ? (
        <section className="card card--escalation">
          <h2 className="card__title">Review changes</h2>
          {preview.errors.length > 0 ? (
            <div>
              {preview.errors.map((err) => (
                <p key={err} className="muted">
                  ✗ {err}
                </p>
              ))}
              <button type="button" className="button button--ghost" onClick={() => setPreview(null)}>
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
                      <td className="mono">{String(d.old_value)}</td>
                      <td className="mono">{String(d.new_value)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
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
                <button type="button" className="button button--ghost" onClick={() => setPreview(null)}>
                  Cancel
                </button>
                <button type="button" className="button button--primary" onClick={handleApply} disabled={applying}>
                  {applying ? 'Applying…' : 'Confirm & Apply'}
                </button>
              </div>
            </>
          )}
        </section>
      ) : (
        <>
          <section className="card">
            <h2 className="card__title">Prometheus &amp; Loki</h2>
            <div className="grid grid--2">
              {FIELDS.map(({ field, label, type }) => (
                <label className="field" key={field}>
                  <span>{label}</span>
                  <input
                    type={type === 'float' ? 'number' : 'text'}
                    step={type === 'float' ? '1' : undefined}
                    min={type === 'float' ? data.bounds[field].min : undefined}
                    max={type === 'float' ? data.bounds[field].max : undefined}
                    className="select"
                    value={draft[field]}
                    onChange={(e) => handleFieldChange(field, e.target.value)}
                  />
                </label>
              ))}
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

          <section className="card">
            <h2 className="card__title">Kubernetes connection (read-only)</h2>
            <p className="muted small">{readOnly.description}</p>
            <dl className="stat-list">
              <div className="stat-list__row">
                <dt>Mode</dt>
                <dd>{k8s.mode}</dd>
              </div>
              <div className="stat-list__row">
                <dt>Namespace</dt>
                <dd>{k8s.namespace}</dd>
              </div>
              <div className="stat-list__row">
                <dt>Reachable</dt>
                <dd>
                  <span
                    className={
                      k8s.available ? 'decision-tag decision-tag--allowed' : 'decision-tag decision-tag--denied'
                    }
                  >
                    {k8s.available ? 'Yes' : `No${k8s.init_error ? ` — ${k8s.init_error}` : ''}`}
                  </span>
                </dd>
              </div>
              <div className="stat-list__row">
                <dt>Prometheus bearer token configured</dt>
                <dd>{readOnly.prometheus_bearer_token_configured ? 'Yes' : 'No'}</dd>
              </div>
              <div className="stat-list__row">
                <dt>Loki bearer token configured</dt>
                <dd>{readOnly.loki_bearer_token_configured ? 'Yes' : 'No'}</dd>
              </div>
            </dl>
          </section>

          <section className="card">
            <h2 className="card__title">Health checks (read-only)</h2>
            <p className="muted small">{readOnly.health_checks.description}</p>
            <p className="muted small">{readOnly.health_checks.note}</p>
          </section>
        </>
      )}
    </div>
  )
}
