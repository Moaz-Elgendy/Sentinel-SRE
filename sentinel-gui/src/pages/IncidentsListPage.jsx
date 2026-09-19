import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { listIncidents } from '../api/incidents.js'
import { extractErrorMessage } from '../api/client.js'
import AlertBanner from '../components/ui/AlertBanner.jsx'
import Button from '../components/ui/Button.jsx'
import Card from '../components/ui/Card.jsx'
import EmptyState, { ErrorState } from '../components/ui/EmptyState.jsx'
import Icon from '../components/ui/Icon.jsx'
import LastUpdated from '../components/ui/LastUpdated.jsx'
import { TableSkeleton } from '../components/ui/Loading.jsx'
import PageHeader from '../components/ui/PageHeader.jsx'
import Pagination from '../components/ui/Pagination.jsx'
import SeverityBadge from '../components/ui/SeverityBadge.jsx'
import StatusPill from '../components/ui/StatusPill.jsx'
import Timestamp from '../components/ui/Timestamp.jsx'
import { usePageTitle } from '../hooks/usePageTitle.js'
import { usePolling } from '../hooks/usePolling.js'
import { titleCase } from '../utils/format.js'
import { effectiveIncidentStatus } from '../utils/status.js'

const STATUS_FILTERS = [
  { value: '', label: 'All statuses' },
  { value: 'open', label: 'Open' },
  { value: 'investigating', label: 'Investigating' },
  { value: 'remediating', label: 'Remediating' },
  { value: 'validating', label: 'Validating' },
  { value: 'resolved', label: 'Resolved' },
  { value: 'escalated', label: 'Escalated' },
  { value: 'auto_resolved', label: 'Auto-resolved' },
]

const SEVERITY_FILTERS = [
  { value: '', label: 'All severities' },
  { value: 'critical', label: 'Critical' },
  { value: 'warning', label: 'Warning' },
  { value: 'info', label: 'Info' },
]

const PAGE_SIZE = 25

export default function IncidentsListPage() {
  usePageTitle('Incidents')
  const navigate = useNavigate()

  // Filters live in the URL, so opening an incident and pressing Back returns
  // to exactly the same filtered, paged view.
  const [params, setParams] = useSearchParams()
  const status = params.get('status') ?? ''
  const severity = params.get('severity') ?? ''
  const query = params.get('q') ?? ''
  const offset = Number(params.get('offset') ?? 0) || 0

  function updateParams(changes) {
    const next = new URLSearchParams(params)
    for (const [key, value] of Object.entries(changes)) {
      if (value === '' || value == null || value === 0) next.delete(key)
      else next.set(key, String(value))
    }
    setParams(next, { replace: true })
  }

  const { data, error, loading, busy, updatedAt, refetch } = usePolling(
    () => listIncidents({ status: status || null, limit: PAGE_SIZE, offset }),
    { intervalMs: 6000, resetKey: `${status}|${offset}` }
  )

  if (loading && !data) return <TableSkeleton label="Loading incidents…" />
  if (!data) {
    return (
      <ErrorState
        title="Couldn't load incidents"
        message={extractErrorMessage(error, 'Sentinel did not respond.')}
        onRetry={refetch}
      />
    )
  }

  // Severity and text search refine the page already loaded; status and paging
  // are handled by the server. The count line says so.
  const needle = query.trim().toLowerCase()
  const rows = data.incidents.filter((incident) => {
    if (severity && incident.severity !== severity) return false
    if (!needle) return true
    return [incident.id, incident.app, incident.phase, incident.status]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(needle))
  })
  const isRefining = Boolean(severity || needle)
  const hasAnyFilter = Boolean(status || isRefining)

  function clearFilters() {
    setParams(new URLSearchParams(), { replace: true })
  }

  return (
    <div className="page">
      <PageHeader
        title="Incidents"
        subtitle="Every incident Sentinel has detected, newest first."
        actions={<LastUpdated updatedAt={updatedAt} stale={Boolean(error)} busy={busy} />}
      />

      {error && (
        <AlertBanner tone="warn" title="Showing the last data received">
          {extractErrorMessage(error, 'Could not refresh incidents.')} Retrying automatically.
        </AlertBanner>
      )}

      <Card flush>
        <div className="toolbar" role="search">
          <div className="input-group toolbar__search">
            <Icon name="search" size={14} />
            <input
              type="search"
              className="input"
              placeholder="Filter this page by ID, app or phase"
              aria-label="Filter incidents on this page by ID, application or phase"
              value={query}
              onChange={(e) => updateParams({ q: e.target.value })}
            />
          </div>
          <label className="toolbar__filter">
            <span className="sr-only">Status</span>
            <select
              className="select"
              value={status}
              onChange={(e) => updateParams({ status: e.target.value, offset: 0 })}
              aria-label="Status"
            >
              {STATUS_FILTERS.map((f) => (
                <option key={f.value} value={f.value}>
                  {f.label}
                </option>
              ))}
            </select>
          </label>
          <label className="toolbar__filter">
            <span className="sr-only">Severity</span>
            <select
              className="select"
              value={severity}
              onChange={(e) => updateParams({ severity: e.target.value })}
              aria-label="Severity"
            >
              {SEVERITY_FILTERS.map((f) => (
                <option key={f.value} value={f.value}>
                  {f.label}
                </option>
              ))}
            </select>
          </label>
          {hasAnyFilter && (
            <Button variant="ghost" size="sm" onClick={clearFilters}>
              Clear filters
            </Button>
          )}
          <span className="toolbar__spacer" />
          <span className="toolbar__count" aria-live="polite">
            {isRefining ? `${rows.length} of ${data.incidents.length} on this page match` : `${data.count} total`}
          </span>
        </div>

        {rows.length === 0 ? (
          hasAnyFilter ? (
            <EmptyState
              icon="search"
              title="No incidents match these filters"
              description={
                isRefining && data.incidents.length > 0
                  ? 'Severity and search only apply to the incidents on this page. Try clearing them, or move to another page.'
                  : 'Try a different status, or clear the filters.'
              }
              action={<Button onClick={clearFilters}>Clear filters</Button>}
            />
          ) : (
            <EmptyState
              title="No incidents recorded yet"
              description="Sentinel hasn't detected an incident. When it does, it appears here with its full timeline."
            />
          )
        ) : (
          <div className="table-wrap" aria-busy={busy || undefined}>
            <table className="table table--interactive table--pin-first">
              <caption className="sr-only">Incidents, newest first</caption>
              <thead>
                <tr>
                  <th scope="col">Incident</th>
                  <th scope="col">Application</th>
                  <th scope="col">Severity</th>
                  <th scope="col">Status</th>
                  <th scope="col">Phase</th>
                  <th scope="col">Started</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((incident) => (
                  <tr key={incident.id} onClick={() => navigate(`/incidents/${incident.id}`)}>
                    <td>
                      <Link to={`/incidents/${incident.id}`} className="link link--mono" onClick={(e) => e.stopPropagation()}>
                        {incident.id}
                      </Link>
                    </td>
                    <td>{incident.app}</td>
                    <td>
                      <SeverityBadge severity={incident.severity} />
                    </td>
                    <td>
                      <StatusPill status={effectiveIncidentStatus(incident)} />
                    </td>
                    <td className="muted">{titleCase(incident.phase)}</td>
                    <td className="muted">
                      <Timestamp epoch={incident.created_at} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <Pagination
          offset={offset}
          pageSize={PAGE_SIZE}
          total={data.count}
          onChange={(next) => updateParams({ offset: next })}
          previousLabel="Newer"
          nextLabel="Older"
        />
      </Card>
    </div>
  )
}
