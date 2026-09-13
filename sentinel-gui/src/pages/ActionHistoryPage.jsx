import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import AlertBanner from '../components/AlertBanner.jsx'
import Spinner from '../components/Spinner.jsx'
import { listActions } from '../api/actions.js'
import { listActionTypes } from '../api/meta.js'
import { extractErrorMessage } from '../api/client.js'
import { usePolling } from '../hooks/usePolling.js'
import { formatDuration, formatTimestamp, formatPercent, titleCase } from '../utils/format.js'

export default function ActionHistoryPage() {
  const [actionFilter, setActionFilter] = useState('')
  const [actionTypes, setActionTypes] = useState([])

  useEffect(() => {
    listActionTypes().then(setActionTypes).catch(() => setActionTypes([]))
  }, [])

  const { data, error, loading } = usePolling(
    () => listActions({ action: actionFilter || null, limit: 200 }),
    { intervalMs: 8000 }
  )

  return (
    <div className="page">
      <div className="page__header">
        <h1>Action History</h1>
        <select className="select" value={actionFilter} onChange={(e) => setActionFilter(e.target.value)}>
          <option value="">All actions</option>
          {actionTypes.map((action) => (
            <option key={action} value={action}>
              {titleCase(action)}
            </option>
          ))}
          <option value="escalate">Escalate</option>
        </select>
      </div>

      {error && <AlertBanner>{extractErrorMessage(error, 'Could not load action history.')}</AlertBanner>}

      {loading ? (
        <Spinner label="Loading action history…" />
      ) : (
        <section className="card">
          {data.actions.length === 0 ? (
            <p className="muted">No actions recorded yet.</p>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Incident</th>
                  <th>App</th>
                  <th>Action</th>
                  <th>Target</th>
                  <th>Confidence</th>
                  <th>Authorization</th>
                  <th>Result</th>
                </tr>
              </thead>
              <tbody>
                {data.actions.map((row, i) => (
                  // eslint-disable-next-line react/no-array-index-key
                  <tr key={i}>
                    <td className="mono">{formatTimestamp(row.at)}</td>
                    <td>
                      <Link to={`/incidents/${row.incident_id}`} className="link link--mono">
                        {row.incident_id}
                      </Link>
                    </td>
                    <td>{row.app}</td>
                    <td>{titleCase(row.action)}</td>
                    <td className="mono">{row.target ?? '—'}</td>
                    <td>{formatPercent(row.confidence)}</td>
                    <td>
                      <span className={`tag ${row.authorization_type === 'autonomous' ? '' : 'tag--warn'}`}>
                        {titleCase(row.authorization_type)}
                      </span>
                    </td>
                    <td>
                      {row.succeeded == null ? (
                        <span className="muted">—</span>
                      ) : row.succeeded ? (
                        <span className="decision-tag decision-tag--allowed">Succeeded</span>
                      ) : (
                        <span className="decision-tag decision-tag--denied">Failed</span>
                      )}
                      {row.duration_seconds != null && (
                        <span className="muted small"> · {formatDuration(row.duration_seconds)}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}
    </div>
  )
}
