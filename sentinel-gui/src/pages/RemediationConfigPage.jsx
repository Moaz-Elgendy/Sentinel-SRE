import { useEffect, useState } from 'react'
import AlertBanner from '../components/AlertBanner.jsx'
import Spinner from '../components/Spinner.jsx'
import { applyRemediationChange, getRemediationConfig, previewRemediationChange } from '../api/config.js'
import { extractErrorMessage } from '../api/client.js'
import { formatRelativeTime, titleCase } from '../utils/format.js'

export default function RemediationConfigPage() {
  const [data, setData] = useState(null)
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
    getRemediationConfig()
      .then((body) => {
        setData(body)
        setLoadError(null)
      })
      .catch((err) => setLoadError(extractErrorMessage(err, 'Could not load remediation configuration.')))
      .finally(() => setLoading(false))
  }

  useEffect(loadConfig, [])

  async function handleToggleDryRun() {
    setPreview(null)
    setActionError(null)
    setReviewing(true)
    try {
      const nextValue = !data.current.dry_run
      const result = await previewRemediationChange({ dry_run: nextValue })
      setPreview({ ...result, changes: { dry_run: nextValue } })
    } catch (err) {
      setActionError(extractErrorMessage(err, 'Could not validate this change.'))
    } finally {
      setReviewing(false)
    }
  }

  async function handleApply() {
    setApplying(true)
    setActionError(null)
    try {
      await applyRemediationChange(preview.changes, reason)
      setPreview(null)
      setReason('')
      setJustApplied(true)
      loadConfig()
    } catch (err) {
      setActionError(extractErrorMessage(err, 'Could not apply this change.'))
    } finally {
      setApplying(false)
    }
  }

  if (loading) return <Spinner label="Loading remediation configuration…" />
  if (loadError) return <AlertBanner>{loadError}</AlertBanner>
  if (!data) return null

  const readOnly = data.read_only

  return (
    <div className="page">
      <div className="page__header">
        <div>
          <h1>Remediation</h1>
          <p className="page__subtitle">
            {data.last_changed_at && (
              <>Last changed {formatRelativeTime(data.last_changed_at)} by {data.last_changed_by}.</>
            )}
          </p>
        </div>
      </div>

      {justApplied && !preview && (
        <AlertBanner tone="success">Configuration applied — Sentinel is using the new value now.</AlertBanner>
      )}
      {actionError && <AlertBanner>{actionError}</AlertBanner>}

      {preview ? (
        <section className="card card--escalation">
          <h2 className="card__title">Review change</h2>
          {preview.errors.length > 0 ? (
            <div>
              {preview.errors.map((err) => (
                <p key={err} className="muted">
                  ✗ {err}
                </p>
              ))}
              <button type="button" className="button button--ghost" onClick={() => setPreview(null)}>
                Cancel
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
            <h2 className="card__title">Dry run mode</h2>
            <p className="muted small">
              When on, Sentinel decides and authorises actions exactly as normal, but does not apply them to the
              cluster — everything else (evidence, RCA, Policy Engine, audit trail) runs for real.
            </p>
            <div className="service-row">
              <span className={data.current.dry_run ? 'decision-tag decision-tag--allowed' : 'decision-tag decision-tag--denied'}>
                {data.current.dry_run ? 'DRY RUN — no cluster mutations' : 'AUTONOMOUS — actions are applied'}
              </span>
              <button type="button" className="button button--ghost" onClick={handleToggleDryRun} disabled={reviewing}>
                {reviewing ? 'Validating…' : data.current.dry_run ? 'Turn dry run off' : 'Turn dry run on'}
              </button>
            </div>
          </section>

          <section className="card">
            <h2 className="card__title">Action ladder (read-only)</h2>
            <p className="muted small">{readOnly.description}</p>
            <table className="table">
              <thead>
                <tr>
                  <th>Root cause</th>
                  <th>Candidate actions, in order</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(readOnly.action_ladder).map(([rootCause, actions]) => (
                  <tr key={rootCause}>
                    <td>{titleCase(rootCause)}</td>
                    <td className="mono">{actions.length > 0 ? actions.map(titleCase).join(' → ') : '— (never remediable)'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="muted small">
              Fallback confidence discount: {readOnly.fallback_confidence_discount} per step down the ladder.
            </p>
          </section>
        </>
      )}
    </div>
  )
}
