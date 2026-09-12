import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import AlertBanner from '../components/AlertBanner'
import Spinner from '../components/Spinner'
import StatusBadge from '../components/StatusBadge'
import { extractErrorMessage } from '../api/client'
import * as requestsApi from '../api/requests'
import * as servicesApi from '../api/services'

export default function RequestsPage() {
  const [requests, setRequests] = useState([])
  const [servicesById, setServicesById] = useState({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)

    Promise.all([requestsApi.listRequests(), servicesApi.listServices()])
      .then(([requestData, serviceData]) => {
        if (cancelled) return
        setRequests(requestData)
        setServicesById(Object.fromEntries(serviceData.map((s) => [s.id, s])))
      })
      .catch((err) => {
        if (!cancelled) setError(extractErrorMessage(err, 'Could not load your requests.'))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [])

  if (loading) return <Spinner label="Loading your requests…" />

  return (
    <div className="page">
      <div className="page-header">
        <h1>My Requests</h1>
        <p className="page-subtitle">Track the status of every service request you've submitted.</p>
      </div>

      <AlertBanner>{error}</AlertBanner>

      {requests.length === 0 && !error ? (
        <div className="empty-state">
          <p>You haven't submitted any requests yet.</p>
          <Link to="/services" className="btn btn-primary">
            Browse services
          </Link>
        </div>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Service</th>
                <th>Status</th>
                <th>Submitted</th>
                <th>Last update</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {requests.map((req) => (
                <tr key={req.id}>
                  <td>{servicesById[req.service_id]?.name ?? 'Unknown service'}</td>
                  <td>
                    <StatusBadge status={req.status} />
                  </td>
                  <td>{new Date(req.submission_date).toLocaleDateString()}</td>
                  <td>{new Date(req.last_update).toLocaleDateString()}</td>
                  <td>
                    <Link to={`/requests/${req.id}`} className="link">
                      View details
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
