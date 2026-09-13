import { useState } from 'react'
import { Link } from 'react-router-dom'
import AlertBanner from '../components/AlertBanner.jsx'
import Spinner from '../components/Spinner.jsx'
import { listIncidents } from '../api/incidents.js'
import { extractErrorMessage } from '../api/client.js'
import { usePolling } from '../hooks/usePolling.js'
import { formatRelativeTime, titleCase } from '../utils/format.js'

const STATUS_FILTERS = [
  { value: '', label: 'All' },
  { value: 'open', label: 'Open' },
  { value: 'investigating', label: 'Investigating' },
  { value: 'remediating', label: 'Remediating' },
  { value: 'validating', label: 'Validating' },
  { value: 'resolved', label: 'Resolved' },
  { value: 'escalated', label: 'Escalated' },
  { value: 'auto_resolved', label: 'Auto-resolved' },
]

const PAGE_SIZE = 25

export default function IncidentsListPage() {
  const [status, setStatus] = useState('')
  const [offset, setOffset] = useState(0)

  const { data, error, loading } = usePolling(
    () => listIncidents({ status: status || null, limit: PAGE_SIZE, offset }),
    { intervalMs: 6000 }
  )

  function handleStatusChange(event) {
    setStatus(event.target.value)
    setOffset(0)
  }

  return (
    <div className="page">
      <div className="page__header">
        <h1>Incidents</h1>
        <select className="select" value={status} onChange={handleStatusChange}>
          {STATUS_FILTERS.map((f) => (
            <option key={f.value} value={f.value}>
              {f.label}
            </option>
          ))}
        </select>
      </div>

      {error && <AlertBanner>{extractErrorMessage(error, 'Could not load incidents.')}</AlertBanner>}

      {loading ? (
        <Spinner label="Loading incidents…" />
      ) : (
        <section className="card">
          {data.incidents.length === 0 ? (
            <p className="muted">No incidents match this filter.</p>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Application</th>
                  <th>Severity</th>
                  <th>Status</th>
                  <th>Phase</th>
                  <th>Started</th>
                </tr>
              </thead>
              <tbody>
                {data.incidents.map((incident) => (
                  <tr key={incident.id}>
                    <td>
                      <Link to={`/incidents/${incident.id}`} className="link link--mono">
                        {incident.id}
                      </Link>
                    </td>
                    <td>{incident.app}</td>
                    <td>
                      <span className={`severity-dot severity-dot--${incident.severity ?? 'unknown'}`} aria-hidden="true" />
                      {titleCase(incident.severity)}
                    </td>
                    <td>{incident.escalated ? 'Escalated' : titleCase(incident.status)}</td>
                    <td>{titleCase(incident.phase)}</td>
                    <td>{formatRelativeTime(incident.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <div className="pagination">
            <button
              type="button"
              className="button button--ghost"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              ← Newer
            </button>
            <span className="pagination__count">
              {data ? `${offset + 1}–${offset + data.incidents.length} of ${data.count}` : ''}
            </span>
            <button
              type="button"
              className="button button--ghost"
              disabled={!data || offset + PAGE_SIZE >= data.count}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              Older →
            </button>
          </div>
        </section>
      )}
    </div>
  )
}
