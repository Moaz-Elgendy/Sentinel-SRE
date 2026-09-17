import { useEffect, useState } from 'react'
import AlertBanner from '../components/AlertBanner.jsx'
import Spinner from '../components/Spinner.jsx'
import { applyAiChange, getAiConfig, previewAiChange } from '../api/config.js'
import { extractErrorMessage } from '../api/client.js'
import { formatRelativeTime, titleCase } from '../utils/format.js'

// `type` drives which input renders below. Timeout bounds come from the
// backend's /api/config/ai `bounds` payload (single source of truth — see
// ai_admin.py's TIMEOUT_BOUNDS), never hardcoded here.
const FIELDS = [
  { field: 'llm_provider', label: 'Provider', type: 'enum' },
  { field: 'openai_model', label: 'OpenAI model', type: 'string' },
  { field: 'openai_timeout_seconds', label: 'OpenAI timeout (seconds)', type: 'float' },
  { field: 'openai_base_url', label: 'OpenAI-compatible base URL (blank = api.openai.com)', type: 'string' },
  { field: 'gemini_model', label: 'Gemini model', type: 'string' },
  { field: 'gemini_timeout_seconds', label: 'Gemini timeout (seconds)', type: 'float' },
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

export default function AiConfigPage() {
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
    getAiConfig()
      .then((body) => {
        setData(body)
        setDraft(Object.fromEntries(FIELDS.map(({ field }) => [field, body.current[field]])))
        setLoadError(null)
      })
      .catch((err) => setLoadError(extractErrorMessage(err, 'Could not load AI/reasoning configuration.')))
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
      const result = await previewAiChange(changes)
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
      await applyAiChange(preview.changes, reason)
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

  if (loading) return <Spinner label="Loading AI/reasoning configuration…" />
  if (loadError) return <AlertBanner>{loadError}</AlertBanner>
  if (!data || !draft) return null

  const readOnly = data.read_only
  const bounds = data.bounds

  return (
    <div className="page">
      <div className="page__header">
        <div>
          <h1>AI &amp; Reasoning</h1>
          <p className="page__subtitle">
            Provider, model, timeout, and base URL for LLM-assisted root cause analysis.{' '}
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
                      <td className="mono">{String(d.old_value) || '(default)'}</td>
                      <td className="mono">{String(d.new_value) || '(default)'}</td>
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
            <h2 className="card__title">Provider &amp; model</h2>
            <div className="grid grid--2">
              {FIELDS.map(({ field, label, type }) => (
                <label className="field" key={field}>
                  <span>{label}</span>
                  {type === 'enum' ? (
                    <select
                      className="select"
                      value={draft[field]}
                      onChange={(e) => handleFieldChange(field, e.target.value)}
                    >
                      {bounds[field].choices.map((choice) => (
                        <option key={choice} value={choice}>
                          {titleCase(choice)}
                        </option>
                      ))}
                    </select>
                  ) : type === 'float' ? (
                    <input
                      type="number"
                      step="1"
                      min={bounds[field].min}
                      max={bounds[field].max}
                      className="select"
                      value={draft[field]}
                      onChange={(e) => handleFieldChange(field, e.target.value)}
                    />
                  ) : (
                    <input
                      type="text"
                      className="select"
                      value={draft[field]}
                      onChange={(e) => handleFieldChange(field, e.target.value)}
                    />
                  )}
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
            <h2 className="card__title">API keys &amp; hardcoded values (read-only)</h2>
            <p className="muted small">{readOnly.description}</p>
            <dl className="stat-list">
              <div className="stat-list__row">
                <dt>OpenAI API key configured</dt>
                <dd>
                  <span
                    className={
                      readOnly.openai_api_key_configured
                        ? 'decision-tag decision-tag--allowed'
                        : 'decision-tag decision-tag--denied'
                    }
                  >
                    {readOnly.openai_api_key_configured ? 'Yes' : 'No — rule-based only if active provider'}
                  </span>
                </dd>
              </div>
              <div className="stat-list__row">
                <dt>Gemini API key configured</dt>
                <dd>
                  <span
                    className={
                      readOnly.gemini_api_key_configured
                        ? 'decision-tag decision-tag--allowed'
                        : 'decision-tag decision-tag--denied'
                    }
                  >
                    {readOnly.gemini_api_key_configured ? 'Yes' : 'No — rule-based only if active provider'}
                  </span>
                </dd>
              </div>
              <div className="stat-list__row">
                <dt>Temperature</dt>
                <dd>
                  {readOnly.temperature} <span className="muted small">— {readOnly.temperature_note}</span>
                </dd>
              </div>
            </dl>
          </section>
        </>
      )}
    </div>
  )
}
