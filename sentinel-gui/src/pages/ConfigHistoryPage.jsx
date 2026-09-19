import { useCallback, useEffect, useId, useState } from 'react'
import { extractErrorMessage } from '../api/client.js'
import { getConfigHistory, restoreConfigChange } from '../api/config.js'
import AlertBanner from '../components/ui/AlertBanner.jsx'
import Button from '../components/ui/Button.jsx'
import Card from '../components/ui/Card.jsx'
import ConfirmDialog from '../components/ui/ConfirmDialog.jsx'
import EmptyState, { ErrorState } from '../components/ui/EmptyState.jsx'
import Icon from '../components/ui/Icon.jsx'
import { TableSkeleton } from '../components/ui/Loading.jsx'
import PageHeader from '../components/ui/PageHeader.jsx'
import Tag from '../components/ui/Tag.jsx'
import { useToast } from '../components/ui/Toast.jsx'
import { usePageTitle } from '../hooks/usePageTitle.js'
import { formatTimestamp, titleCase } from '../utils/format.js'

// Kept in one place, in registration order, so a new admin category only
// needs one line added here to show up in the filter — mirrors
// app/routers/config.py's own _CATEGORIES registry in spirit.
const CATEGORIES = ['policy', 'rca', 'remediation', 'ai', 'monitoring']

function formatValue(value) {
  if (Array.isArray(value)) return value.join(', ') || '—'
  return String(value)
}

function ChangeRow({ entry, onRestored }) {
  const toast = useToast()
  const bodyId = useId()
  const [expanded, setExpanded] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [restoring, setRestoring] = useState(false)
  const [error, setError] = useState(null)

  const fields = entry.changes.map((c) => c.field).join(', ')

  async function handleRestore() {
    setRestoring(true)
    setError(null)
    try {
      await restoreConfigChange(entry.id)
      setConfirming(false)
      toast(`Restored ${fields} to its previous value`)
      onRestored()
    } catch (err) {
      setConfirming(false)
      setError(extractErrorMessage(err, 'Could not restore this change.'))
    } finally {
      setRestoring(false)
    }
  }

  return (
    <li className="history-entry">
      <button
        type="button"
        className="history-entry__header"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        aria-controls={bodyId}
      >
        <Icon name={expanded ? 'chevronDown' : 'chevronRight'} size={14} />
        <span className="history-entry__time mono">{formatTimestamp(entry.created_at)}</span>
        <span className="history-entry__admin">{entry.admin_id}</span>
        <Tag>{titleCase(entry.category)}</Tag>
        <span className="history-entry__fields mono truncate">{fields}</span>
        {entry.restores_change_id && <Tag tone="info">Restore</Tag>}
        <Tag tone={entry.status === 'applied' ? 'ok' : 'bad'}>{titleCase(entry.status)}</Tag>
      </button>

      {expanded && (
        <div className="history-entry__body" id={bodyId}>
          <div className="stack">
            {entry.reason && (
              <p>
                <span className="muted">Reason:</span> “{entry.reason}”
              </p>
            )}
            {entry.detail && <p className="muted">{entry.detail}</p>}

            <div className="card card--flush table-wrap">
              <table className="table">
                <caption className="sr-only">Fields changed</caption>
                <thead>
                  <tr>
                    <th scope="col">Setting</th>
                    <th scope="col">Before</th>
                    <th scope="col">After</th>
                  </tr>
                </thead>
                <tbody>
                  {entry.changes.map((c) => (
                    <tr key={c.field}>
                      <td>{titleCase(c.field)}</td>
                      <td className="mono muted">{formatValue(c.old_value)}</td>
                      <td className="mono diff__new">{formatValue(c.new_value)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {error && <AlertBanner>{error}</AlertBanner>}
            {entry.status === 'applied' && (
              <div>
                <Button icon="history" onClick={() => setConfirming(true)}>
                  Restore this change
                </Button>
              </div>
            )}
          </div>
        </div>
      )}

      <ConfirmDialog
        open={confirming}
        title="Restore this change?"
        confirmLabel="Restore change"
        busy={restoring}
        onConfirm={handleRestore}
        onCancel={() => setConfirming(false)}
      >
        <p>
          This reverts <strong className="mono">{fields}</strong> to the previous value
          {entry.changes.length > 1 ? 's' : ''}. It is applied as a new, audited change — the history is never rewritten.
        </p>
      </ConfirmDialog>
    </li>
  )
}

export default function ConfigHistoryPage() {
  usePageTitle('Configuration history')
  const [category, setCategory] = useState('')
  const [query, setQuery] = useState('')
  const [history, setHistory] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  // Only the very first load shows a skeleton; after that the list stays on
  // screen while a filter change or a restore refreshes it.
  const refetch = useCallback(() => {
    return getConfigHistory({ category: category || null })
      .then((data) => {
        setHistory(data)
        setError(null)
      })
      .catch((err) => setError(err))
      .finally(() => setLoading(false))
  }, [category])

  useEffect(() => {
    refetch()
  }, [refetch])

  if (loading && !history) return <TableSkeleton label="Loading configuration history…" rows={6} />
  if (!history) {
    return (
      <ErrorState
        title="Couldn't load configuration history"
        message={extractErrorMessage(error, 'Sentinel did not respond.')}
        onRetry={() => {
          setLoading(true)
          refetch()
        }}
      />
    )
  }

  const needle = query.trim().toLowerCase()
  const entries = history.filter((entry) => {
    if (!needle) return true
    return [entry.admin_id, entry.reason, ...entry.changes.map((c) => c.field)]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(needle))
  })

  return (
    <div className="page">
      <PageHeader
        title="Configuration history"
        subtitle="Every change made through the Administration pages: who, when, what, and why. Any applied change can be restored."
      />

      {error && (
        <AlertBanner tone="warn" title="Showing the last data received">
          {extractErrorMessage(error, 'Could not refresh configuration history.')}
        </AlertBanner>
      )}

      <Card flush>
        <div className="toolbar" role="search">
          <div className="input-group toolbar__search">
            <Icon name="search" size={14} />
            <input
              type="search"
              className="input"
              placeholder="Search admin, setting or reason"
              aria-label="Search configuration history by admin, setting or reason"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
          <select className="select" aria-label="Category" value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="">All categories</option>
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {titleCase(c)}
              </option>
            ))}
          </select>
          <span className="toolbar__spacer" />
          <span className="toolbar__count" aria-live="polite">
            {entries.length} {entries.length === 1 ? 'change' : 'changes'}
          </span>
        </div>

        {entries.length === 0 ? (
          <EmptyState
            title={
              needle
                ? 'No changes match your search'
                : category
                  ? `No ${titleCase(category)} changes recorded yet`
                  : 'No configuration changes recorded yet'
            }
            description={
              needle
                ? 'Try a different admin, setting or reason.'
                : 'When an administrator applies a change on a configuration page, it is recorded here.'
            }
            compact
          />
        ) : (
          <ul className="history-list">
            {entries.map((entry) => (
              <ChangeRow key={entry.id} entry={entry} onRestored={refetch} />
            ))}
          </ul>
        )}
      </Card>
    </div>
  )
}
