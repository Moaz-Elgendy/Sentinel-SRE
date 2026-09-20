import { useState } from 'react'
import { extractErrorMessage } from '../api/client.js'
import AlertBanner from '../components/ui/AlertBanner.jsx'
import Button from '../components/ui/Button.jsx'
import Card from '../components/ui/Card.jsx'
import EmptyState from '../components/ui/EmptyState.jsx'
import Icon from '../components/ui/Icon.jsx'
import LiveBadge from '../components/ui/LiveBadge.jsx'
import { TableSkeleton } from '../components/ui/Loading.jsx'
import PageHeader from '../components/ui/PageHeader.jsx'
import Tag from '../components/ui/Tag.jsx'
import { useLogsStream } from '../hooks/useLogsStream.js'
import { usePageTitle } from '../hooks/usePageTitle.js'
import { formatClock } from '../utils/format.js'

const LEVELS = ['', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']

const LEVEL_TONE = {
  DEBUG: 'neutral',
  INFO: 'info',
  WARNING: 'warn',
  ERROR: 'bad',
  CRITICAL: 'bad',
}

function LogRow({ entry }) {
  return (
    <li className="log-row">
      <time className="log-row__time mono">{formatClock(entry.timestamp_epoch)}</time>
      <Tag tone={LEVEL_TONE[entry.level] ?? 'neutral'} className="log-row__level">
        {entry.level ?? '—'}
      </Tag>
      <span className="log-row__source mono">{entry.name ?? '—'}</span>
      <span className="log-row__message">{entry.message}</span>
      {entry.incident_id && (
        <span className="log-row__incident mono">{entry.incident_id}</span>
      )}
    </li>
  )
}

export default function SentinelLogsPage() {
  usePageTitle('Sentinel Logs')

  const [level, setLevel] = useState('')
  const [component, setComponent] = useState('')
  const [incidentId, setIncidentId] = useState('')
  const [query, setQuery] = useState('')

  const { lines, loading, error, connected, paused, setPaused, pendingCount, clearView } = useLogsStream({
    level: level || undefined,
    component: component || undefined,
    incidentId: incidentId || undefined,
    q: query || undefined,
  })

  const hasFilter = Boolean(level || component || incidentId || query)

  function clearFilters() {
    setLevel('')
    setComponent('')
    setIncidentId('')
    setQuery('')
  }

  if (loading && lines.length === 0) return <TableSkeleton label="Loading Sentinel logs…" />

  return (
    <div className="page">
      <PageHeader
        title="Sentinel Logs"
        subtitle="Sentinel's own backend logs — startup, evidence collection, RCA, policy, remediation, escalation, and errors. Not the monitored application's logs."
        actions={connected && !paused ? <LiveBadge /> : null}
      />

      {error && (
        <AlertBanner tone="warn" title="Couldn't load persisted logs">
          {extractErrorMessage(error, 'Sentinel did not respond.')} The live tail above still works if connected.
        </AlertBanner>
      )}

      <Card flush>
        <div className="toolbar" role="search">
          <div className="input-group toolbar__search">
            <Icon name="search" size={14} />
            <input
              type="search"
              className="input"
              placeholder="Search log messages"
              aria-label="Search log messages"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
          <select className="select" aria-label="Level" value={level} onChange={(e) => setLevel(e.target.value)}>
            {LEVELS.map((l) => (
              <option key={l || 'any'} value={l}>
                {l || 'Any level'}
              </option>
            ))}
          </select>
          <input
            type="text"
            className="input"
            style={{ maxWidth: 200 }}
            placeholder="Component (e.g. rca)"
            aria-label="Filter by component"
            value={component}
            onChange={(e) => setComponent(e.target.value)}
          />
          <input
            type="text"
            className="input"
            style={{ maxWidth: 160 }}
            placeholder="Incident ID"
            aria-label="Filter by incident id"
            value={incidentId}
            onChange={(e) => setIncidentId(e.target.value)}
          />
          {hasFilter && (
            <Button variant="ghost" size="sm" onClick={clearFilters}>
              Clear filters
            </Button>
          )}
          <span className="toolbar__spacer" />
          <Button
            variant={paused ? 'primary' : 'secondary'}
            size="sm"
            icon={paused ? 'eyeOff' : 'eye'}
            onClick={() => setPaused((p) => !p)}
          >
            {paused ? `Resume${pendingCount ? ` (${pendingCount} new)` : ''}` : 'Pause'}
          </Button>
          <Button variant="ghost" size="sm" icon="x" onClick={clearView}>
            Clear view
          </Button>
        </div>

        {lines.length === 0 ? (
          hasFilter ? (
            <EmptyState
              icon="search"
              title="No log lines match these filters"
              description="Try a different level, component or search term, or clear the filters."
              action={<Button onClick={clearFilters}>Clear filters</Button>}
            />
          ) : (
            <EmptyState
              icon="terminal"
              title="No logs captured yet"
              description="Sentinel's own logs will appear here as it runs — clearing this view never deletes the persisted log file."
            />
          )
        ) : (
          <ul className="log-feed">
            {lines.map((entry, index) => (
              // eslint-disable-next-line react/no-array-index-key
              <LogRow key={index} entry={entry} />
            ))}
          </ul>
        )}
      </Card>
    </div>
  )
}
