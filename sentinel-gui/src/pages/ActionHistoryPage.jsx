import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { listActions } from '../api/actions.js'
import { extractErrorMessage } from '../api/client.js'
import { listActionTypes } from '../api/meta.js'
import AlertBanner from '../components/ui/AlertBanner.jsx'
import Button from '../components/ui/Button.jsx'
import Card from '../components/ui/Card.jsx'
import EmptyState, { ErrorState } from '../components/ui/EmptyState.jsx'
import Icon from '../components/ui/Icon.jsx'
import LastUpdated from '../components/ui/LastUpdated.jsx'
import { TableSkeleton } from '../components/ui/Loading.jsx'
import PageHeader from '../components/ui/PageHeader.jsx'
import Pagination from '../components/ui/Pagination.jsx'
import Tag from '../components/ui/Tag.jsx'
import Timestamp from '../components/ui/Timestamp.jsx'
import { usePageTitle } from '../hooks/usePageTitle.js'
import { usePolling } from '../hooks/usePolling.js'
import { formatDuration, formatPercent, titleCase } from '../utils/format.js'

const PAGE_SIZE = 25
const FETCH_LIMIT = 200

const RESULT_FILTERS = [
  { value: '', label: 'Any result' },
  { value: 'succeeded', label: 'Succeeded' },
  { value: 'failed', label: 'Failed' },
  { value: 'none', label: 'Not executed' },
]

function resultOf(row) {
  if (row.succeeded == null) return 'none'
  return row.succeeded ? 'succeeded' : 'failed'
}

function ResultCell({ row }) {
  const result = resultOf(row)
  return (
    <>
      {result === 'none' && <span className="faint">—</span>}
      {result === 'succeeded' && <Tag tone="ok">Succeeded</Tag>}
      {result === 'failed' && <Tag tone="bad">Failed</Tag>}
      {row.duration_seconds != null && (
        <span className="table__secondary">{formatDuration(row.duration_seconds)}</span>
      )}
    </>
  )
}

export default function ActionHistoryPage() {
  usePageTitle('Action history')
  const navigate = useNavigate()
  const [actionFilter, setActionFilter] = useState('')
  const [actionTypes, setActionTypes] = useState([])
  const [authFilter, setAuthFilter] = useState('')
  const [resultFilter, setResultFilter] = useState('')
  const [query, setQuery] = useState('')
  const [offset, setOffset] = useState(0)

  useEffect(() => {
    listActionTypes()
      .then(setActionTypes)
      .catch(() => setActionTypes([]))
  }, [])

  const { data, error, loading, busy, updatedAt, refetch } = usePolling(
    () => listActions({ action: actionFilter || null, limit: FETCH_LIMIT }),
    { intervalMs: 8000, resetKey: actionFilter }
  )

  if (loading && !data) return <TableSkeleton label="Loading action history…" />
  if (!data) {
    return (
      <ErrorState
        title="Couldn't load action history"
        message={extractErrorMessage(error, 'Sentinel did not respond.')}
        onRetry={refetch}
      />
    )
  }

  // The API returns up to FETCH_LIMIT most recent actions in one response, so
  // everything below refines and pages that set locally.
  const authTypes = [...new Set(data.actions.map((row) => row.authorization_type).filter(Boolean))]
  const needle = query.trim().toLowerCase()
  const filtered = data.actions.filter((row) => {
    if (authFilter && row.authorization_type !== authFilter) return false
    if (resultFilter && resultOf(row) !== resultFilter) return false
    if (!needle) return true
    return [row.incident_id, row.app, row.target, row.action]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(needle))
  })
  const pageRows = filtered.slice(offset, offset + PAGE_SIZE)
  const hasFilter = Boolean(actionFilter || authFilter || resultFilter || needle)

  function clearFilters() {
    setActionFilter('')
    setAuthFilter('')
    setResultFilter('')
    setQuery('')
    setOffset(0)
  }

  // Any filter change returns to the first page.
  const withReset = (setter) => (event) => {
    setter(event.target.value)
    setOffset(0)
  }

  return (
    <div className="page">
      <PageHeader
        title="Action history"
        subtitle="Every remediation Sentinel decided on, who or what authorised it, and how it turned out."
        actions={<LastUpdated updatedAt={updatedAt} stale={Boolean(error)} busy={busy} />}
      />

      {error && (
        <AlertBanner tone="warn" title="Showing the last data received">
          {extractErrorMessage(error, 'Could not refresh action history.')} Retrying automatically.
        </AlertBanner>
      )}

      <Card flush>
        <div className="toolbar" role="search">
          <div className="input-group toolbar__search">
            <Icon name="search" size={14} />
            <input
              type="search"
              className="input"
              placeholder="Search incident, app or target"
              aria-label="Search actions by incident, application or target"
              value={query}
              onChange={withReset(setQuery)}
            />
          </div>
          <select className="select" aria-label="Action" value={actionFilter} onChange={withReset(setActionFilter)}>
            <option value="">All actions</option>
            {actionTypes.map((action) => (
              <option key={action} value={action}>
                {titleCase(action)}
              </option>
            ))}
            <option value="escalate">Escalate</option>
          </select>
          <select className="select" aria-label="Authorization" value={authFilter} onChange={withReset(setAuthFilter)}>
            <option value="">Any authorization</option>
            {authTypes.map((type) => (
              <option key={type} value={type}>
                {titleCase(type)}
              </option>
            ))}
          </select>
          <select className="select" aria-label="Result" value={resultFilter} onChange={withReset(setResultFilter)}>
            {RESULT_FILTERS.map((f) => (
              <option key={f.value} value={f.value}>
                {f.label}
              </option>
            ))}
          </select>
          {hasFilter && (
            <Button variant="ghost" size="sm" onClick={clearFilters}>
              Clear filters
            </Button>
          )}
          <span className="toolbar__spacer" />
          <span className="toolbar__count" aria-live="polite">
            {filtered.length} of {data.actions.length}
            {data.actions.length >= FETCH_LIMIT ? ` (latest ${FETCH_LIMIT})` : ''}
          </span>
        </div>

        {filtered.length === 0 ? (
          hasFilter ? (
            <EmptyState
              icon="search"
              title="No actions match these filters"
              description="Try a different action or result, or clear the filters."
              action={<Button onClick={clearFilters}>Clear filters</Button>}
            />
          ) : (
            <EmptyState
              title="No actions recorded yet"
              description="When Sentinel restarts, rolls back or scales something — or escalates instead — it is recorded here."
            />
          )
        ) : (
          <div className="table-wrap" aria-busy={busy || undefined}>
            <table className="table table--interactive table--pin-first">
              <caption className="sr-only">Remediation actions, newest first</caption>
              <thead>
                <tr>
                  <th scope="col">Time</th>
                  <th scope="col">Action</th>
                  <th scope="col">Incident</th>
                  <th scope="col" className="num">
                    Confidence
                  </th>
                  <th scope="col">Authorization</th>
                  <th scope="col">Result</th>
                </tr>
              </thead>
              <tbody>
                {pageRows.map((row, i) => (
                  <tr
                    // eslint-disable-next-line react/no-array-index-key
                    key={`${row.incident_id}-${row.at}-${i}`}
                    onClick={() => navigate(`/incidents/${row.incident_id}`)}
                  >
                    <td>
                      <Timestamp epoch={row.at} />
                    </td>
                    <td>
                      {titleCase(row.action)}
                      {row.target && <span className="table__secondary mono">{row.target}</span>}
                    </td>
                    <td>
                      <Link
                        to={`/incidents/${row.incident_id}`}
                        className="link link--mono"
                        onClick={(e) => e.stopPropagation()}
                      >
                        {row.incident_id}
                      </Link>
                      <span className="table__secondary">{row.app}</span>
                    </td>
                    <td className="num">{formatPercent(row.confidence)}</td>
                    <td>
                      <Tag tone={row.authorization_type === 'autonomous' ? 'neutral' : 'warn'}>
                        {titleCase(row.authorization_type)}
                      </Tag>
                    </td>
                    <td>
                      <ResultCell row={row} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <Pagination offset={offset} pageSize={PAGE_SIZE} total={filtered.length} onChange={setOffset} />
      </Card>
    </div>
  )
}
