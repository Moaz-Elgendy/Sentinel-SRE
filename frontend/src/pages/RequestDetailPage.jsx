import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import AlertBanner from '../components/AlertBanner'
import Spinner from '../components/Spinner'
import StatusBadge from '../components/StatusBadge'
import { extractErrorMessage } from '../api/client'
import * as requestsApi from '../api/requests'
import * as servicesApi from '../api/services'

export default function RequestDetailPage() {
  const { requestId } = useParams()

  const [request, setRequest] = useState(null)
  const [service, setService] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)

    requestsApi
      .getRequest(requestId)
      .then(async (req) => {
        if (cancelled) return
        setRequest(req)
        const svc = await servicesApi.getService(req.service_id)
        if (!cancelled) setService(svc)
      })
      .catch((err) => {
        if (!cancelled) setError(extractErrorMessage(err, 'Could not load this request.'))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [requestId])

  if (loading) return <Spinner label="Loading request…" />

  if (error) {
    return (
      <div className="page">
        <AlertBanner>{error}</AlertBanner>
        <Link to="/requests" className="link">
          &larr; Back to My Requests
        </Link>
      </div>
    )
  }

  if (!request) return null

  return (
    <div className="page">
      <Link to="/requests" className="link">
        &larr; Back to My Requests
      </Link>

      <div className="page-header">
        <h1>{service?.name ?? 'Service request'}</h1>
        <StatusBadge status={request.status} />
      </div>

      <div className="detail-grid">
        <section className="detail-card">
          <h2>Request details</h2>
          <dl className="detail-list">
            <div>
              <dt>Submitted</dt>
              <dd>{new Date(request.submission_date).toLocaleString()}</dd>
            </div>
            <div>
              <dt>Last updated</dt>
              <dd>{new Date(request.last_update).toLocaleString()}</dd>
            </div>
            <div>
              <dt>Request ID</dt>
              <dd className="detail-mono">{request.id}</dd>
            </div>
          </dl>

          {request.employee_note && (
            <>
              <h3>Caseworker note</h3>
              <p className="detail-note">{request.employee_note}</p>
            </>
          )}
        </section>

        {service && (
          <section className="detail-card">
            <h2>About this service</h2>
            <p>{service.description}</p>
            {service.required_documents?.length > 0 && (
              <>
                <h3>Required documents</h3>
                <ul>
                  {service.required_documents.map((doc) => (
                    <li key={doc}>{doc}</li>
                  ))}
                </ul>
              </>
            )}
            <p className="service-processing">Estimated processing time: {service.estimated_processing_days} days</p>
          </section>
        )}
      </div>
    </div>
  )
}
