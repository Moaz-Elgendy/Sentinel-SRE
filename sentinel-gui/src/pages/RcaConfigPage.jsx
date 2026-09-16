import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import AlertBanner from '../components/AlertBanner.jsx'
import Spinner from '../components/Spinner.jsx'
import { applyRcaChange, getRcaConfig, previewRcaChange } from '../api/config.js'
import { extractErrorMessage } from '../api/client.js'
import { formatRelativeTime, titleCase } from '../utils/format.js'

const FIELDS = [
  { field: 'max_error_rate', label: 'Max error rate (correlation + validation)' },
  { field: 'max_p95_seconds', label: 'Max P95 latency (seconds)' },
  { field: 'max_cpu_cores', label: 'Max CPU (cores)' },
  { field: 'max_memory_bytes', label: 'Max memory (bytes)' },
  { field: 'settle_seconds', label: 'Settle period before validating (seconds)' },
  { field: 'timeout_seconds', label: 'Validation timeout (seconds)' },
  { field: 'poll_interval_seconds', label: 'Validation poll interval (seconds)' },
]

function diffChanges(current, draft) {
  const changes = {}
  for (const { field } of FIELDS) {
    const draftValue = Number(draft[field])
    if (!Number.isNaN(draftValue) && draftValue !== current[field]) {
      changes[field] = draftValue
    }
  }
  return changes
}

export default function RcaConfigPage() {
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
    getRcaConfig()
      .then((body) => {
        setData(body)
        setDraft(Object.fromEntries(FIELDS.map(({ field }) => [field, body.current[field]])))
        setLoadError(null)
      })
      .catch((err) => setLoadError(extractErrorMessage(err, 'Could not load RCA configuration.')))
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
      const result = await previewRcaChange(changes)
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
      await applyRcaChange(preview.changes, reason)
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

  if (loading) return <Spinner label="Loading RCA configuration…" />
  if (loadError) return <AlertBanner>{loadError}</AlertBanner>
  if (!data || !draft) return null

  const readOnly = data.read_only

  return (
    <div className="page">
      <div className="page__header">
        <div>
          <h1>RCA &amp; Diagnosis</h1>
          <p className="page__subtitle">
            Evidence and recovery-validation thresholds — live on Sentinel's real correlation and
            validation steps.{' '}
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
                      <td className="mono">{d.old_value}</td>
                      <td className="mono">{d.new_value}</td>
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
            <h2 className="card__title">Evidence &amp; validation thresholds</h2>
            <div className="grid grid--2">
              {FIELDS.map(({ field, label }) => (
                <label className="field" key={field}>
                  <span>{label}</span>
                  <input
                    type="number"
                    step={field === 'max_error_rate' ? '0.01' : '1'}
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

          <button
            type="button"
            className="button button--primary"
            onClick={handleReview}
            disabled={reviewing || Object.keys(diffChanges(data.current, draft)).length === 0}
          >
            {reviewing ? 'Validating…' : 'Review changes'}
          </button>

          <section className="card">
            <h2 className="card__title">What controls Sentinel's diagnosis (read-only)</h2>
            <p className="muted small">{readOnly.rule_based_detection.description}</p>
            <dl className="stat-list">
              <div className="stat-list__row">
                <dt>LLM confidence ceiling</dt>
                <dd>{readOnly.rule_based_detection.llm_confidence_ceiling}</dd>
              </div>
              <div className="stat-list__row">
                <dt>LLM confidence delta cap</dt>
                <dd>{readOnly.rule_based_detection.llm_confidence_delta_cap}</dd>
              </div>
              <div className="stat-list__row">
                <dt>Rule confidence max</dt>
                <dd>{readOnly.rule_based_detection.rule_confidence_max}</dd>
              </div>
              <div className="stat-list__row">
                <dt>Deployment correlation window</dt>
                <dd>
                  {readOnly.deployment_correlation_window_minutes.value} min — edit via{' '}
                  <Link to="/policies" className="link">
                    Policies
                  </Link>
                </dd>
              </div>
            </dl>

            <h3>Root cause taxonomy</h3>
            <div className="tag-list">
              {readOnly.root_causes.map((rc) => (
                <span key={rc} className="tag tag--muted">
                  {titleCase(rc)}
                </span>
              ))}
            </div>
          </section>
        </>
      )}
    </div>
  )
}
