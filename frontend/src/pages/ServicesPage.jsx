import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import AlertBanner from '../components/AlertBanner'
import Spinner from '../components/Spinner'
import { extractErrorMessage } from '../api/client'
import * as requestsApi from '../api/requests'
import * as servicesApi from '../api/services'
import { useAuth } from '../context/AuthContext'

export default function ServicesPage() {
  const { isAuthenticated } = useAuth()
  const navigate = useNavigate()

  const [services, setServices] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [requestingId, setRequestingId] = useState(null)
  const [notice, setNotice] = useState(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    servicesApi
      .listServices()
      .then((data) => {
        if (!cancelled) setServices(data)
      })
      .catch((err) => {
        if (!cancelled) setError(extractErrorMessage(err, 'Could not load services.'))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  async function handleRequest(service) {
    if (!isAuthenticated) {
      navigate('/login', { state: { from: { pathname: '/services' } } })
      return
    }
    setError(null)
    setNotice(null)
    setRequestingId(service.id)
    try {
      await requestsApi.createRequest(service.id)
      setNotice(`Request submitted for "${service.name}". You can track it under My Requests.`)
    } catch (err) {
      setError(extractErrorMessage(err, 'Could not submit that request.'))
    } finally {
      setRequestingId(null)
    }
  }

  if (loading) return <Spinner label="Loading services…" />

  return (
    <div className="page">
      <div className="page-header">
        <h1>Government Services</h1>
        <p className="page-subtitle">Browse available services and submit a request.</p>
      </div>

      <AlertBanner>{error}</AlertBanner>
      <AlertBanner tone="success">{notice}</AlertBanner>

      <div className="service-grid">
        {services.map((service) => (
          <article key={service.id} className="service-card">
            <h2>{service.name}</h2>
            <p className="service-description">{service.description}</p>

            {service.required_documents?.length > 0 && (
              <div className="service-documents">
                <span className="service-documents-label">Required documents</span>
                <ul>
                  {service.required_documents.map((doc) => (
                    <li key={doc}>{doc}</li>
                  ))}
                </ul>
              </div>
            )}

            <div className="service-card-footer">
              <span className="service-processing">
                ~{service.estimated_processing_days} day{service.estimated_processing_days === 1 ? '' : 's'} to
                process
              </span>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => handleRequest(service)}
                disabled={requestingId === service.id}
              >
                {requestingId === service.id ? 'Submitting…' : 'Request this service'}
              </button>
            </div>
          </article>
        ))}
      </div>

      {services.length === 0 && <p>No services are available right now.</p>}
    </div>
  )
}
