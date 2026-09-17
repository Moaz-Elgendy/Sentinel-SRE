import { useEffect, useState } from 'react'
import AlertBanner from '../components/AlertBanner.jsx'
import Spinner from '../components/Spinner.jsx'
import { getConfigHistory, restoreConfigChange } from '../api/config.js'
import { extractErrorMessage } from '../api/client.js'
import { formatTimestamp, titleCase } from '../utils/format.js'

function ChangeRow({ entry, onRestored }) {
  const [expanded, setExpanded] = useState(false)
  const [restoring, setRestoring] = useState(false)
  const [error, setError] = useState(null)

  async function handleRestore() {
    const confirmed = window.confirm(
      `Restore will revert this specific change (${entry.changes.map((c) => c.field).join(', ')}) back to its previous value(s), as a new, audited change. Continue?`
    )
    if (!confirmed) return
    setRestoring(true)
    setError(null)
    try {
      await restoreConfigChange(entry.id)
      onRestored()
    } catch (err) {
      setError(extractErrorMessage(err, 'Could not restore this change.'))
    } finally {
      setRestoring(false)
    }
  }

  return (
    <div className="history-entry">
      <button type="button" className="history-entry__header" onClick={() => setExpanded((v) => !v)}>
        <span className="mono">{formatTimestamp(entry.created_at)}</span>
        <span>{entry.admin_id}</span>
        <span className="tag tag--muted">{titleCase(entry.category)}</span>
        <span className="mono">{entry.changes.map((c) => c.field).join(', ')}</span>
        {entry.restores_change_id && <span className="tag">restore</span>}
        <span
          className={
            entry.status === 'applied' ? 'decision-tag decision-tag--allowed' : 'decision-tag decision-tag--denied'
          }
        >
          {titleCase(entry.status)}
        </span>
      </button>

      {expanded && (
        <div className="history-entry__body">
          {entry.reason && <p className="muted">Reason: "{entry.reason}"</p>}
          {entry.detail && <p className="muted">{entry.detail}</p>}
          <table className="table">
            <thead>
              <tr>
                <th>Field</th>
                <th>Before</th>
                <th>After</th>
              </tr>
            </thead>
            <tbody>
              {entry.changes.map((c) => (
                <tr key={c.field}>
                  <td>{titleCase(c.field)}</td>
                  <td className="mono">{Array.isArray(c.old_value) ? c.old_value.join(', ') || '—' : String(c.old_value)}</td>
                  <td className="mono">{Array.isArray(c.new_value) ? c.new_value.join(', ') || '—' : String(c.new_value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {error && <AlertBanner>{error}</AlertBanner>}
          {entry.status === 'applied' && (
            <button type="button" className="button button--ghost" onClick={handleRestore} disabled={restoring}>
              {restoring ? 'Restoring…' : 'Restore this change'}
            </button>
          )}
        </div>
      )}
    </div>
  )
}

// Kept in one place, in registration order, so a new admin category only
// needs one line added here to show up in the filter — mirrors
// app/routers/config.py's own _CATEGORIES registry in spirit.
const CATEGORIES = ['policy', 'rca', 'remediation', 'ai', 'monitoring']

export default function ConfigHistoryPage() {
  const [category, setCategory] = useState('')
  const [history, setHistory] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  function refetch() {
    setLoading(true)
    getConfigHistory({ category: category || null })
      .then((data) => {
        setHistory(data)
        setError(null)
      })
      .catch((err) => setError(err))
      .finally(() => setLoading(false))
  }

  useEffect(refetch, [category])

  if (loading) return <Spinner label="Loading configuration history…" />
  if (error) return <AlertBanner>{extractErrorMessage(error, 'Could not load configuration history.')}</AlertBanner>

  return (
    <div className="page">
      <div className="page__header">
        <h1>Configuration History</h1>
        <label className="field field--inline">
          <span>Category</span>
          <select className="select" value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="">All</option>
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {titleCase(c)}
              </option>
            ))}
          </select>
        </label>
      </div>
      <section className="card">
        {!history || history.length === 0 ? (
          <p className="muted">
            {category ? `No ${titleCase(category)} changes recorded yet.` : 'No configuration changes recorded yet.'}
          </p>
        ) : (
          <div className="history-list">
            {history.map((entry) => (
              <ChangeRow key={entry.id} entry={entry} onRestored={refetch} />
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
