import { useState } from 'react'
import { Link } from 'react-router-dom'
import AlertBanner from '../components/AlertBanner.jsx'
import Spinner from '../components/Spinner.jsx'
import StatusPill from '../components/StatusPill.jsx'
import { getDashboardSummary } from '../api/dashboard.js'
import { listEnvironments, testEnvironmentConnection } from '../api/environments.js'
import { extractErrorMessage } from '../api/client.js'
import { usePolling } from '../hooks/usePolling.js'
import { formatRelativeTime, titleCase } from '../utils/format.js'

function ConnectorRow({ name, result }) {
  if (!result) return null
  const status = result.ok === null ? 'unknown' : result.ok ? 'operational' : 'critical'
  const label = result.ok === null ? 'Not configured' : result.ok ? 'Reachable' : 'Unreachable'
  return (
    <div className="stat-list__row">
      <dt>{name}</dt>
      <dd>
        <StatusPill status={status} label={label} />
        {result.detail && <div className="muted small">{result.detail}</div>}
      </dd>
    </div>
  )
}

function EnvironmentCard({ environment, services, recentIncidents }) {
  const [testResult, setTestResult] = useState(null)
  const [testing, setTesting] = useState(false)
  const [testError, setTestError] = useState(null)

  async function handleTestConnection() {
    setTesting(true)
    setTestError(null)
    try {
      const result = await testEnvironmentConnection(environment.id)
      setTestResult(result)
    } catch (err) {
      setTestError(extractErrorMessage(err, 'Could not run the connectivity test.'))
    } finally {
      setTesting(false)
    }
  }

  return (
    <section className="card">
      <div className="card__title-row">
        <div>
          <h2 className="card__title">
            {environment.customer_id} <span className="muted">/</span> {environment.name}
          </h2>
          <p className="muted small">
            Kubernetes: {environment.kubernetes.mode} · namespace {environment.kubernetes.namespace}
          </p>
        </div>
        <button type="button" className="button button--ghost" onClick={handleTestConnection} disabled={testing}>
          {testing ? 'Testing…' : 'Test connectivity'}
        </button>
      </div>

      {testError && <AlertBanner>{testError}</AlertBanner>}

      <div className="grid grid--2">
        <div>
          <h3>Services</h3>
          <div className="service-list">
            {services.length === 0 ? (
              <p className="muted">No services configured for this environment.</p>
            ) : (
              services.map((service) => (
                <div className="service-row" key={service.name}>
                  <span className="service-row__icon" aria-hidden="true">
                    {service.healthy === true ? '🟢' : service.healthy === false ? '🔴' : '⚪'}
                  </span>
                  <span className="service-row__name">{service.name}</span>
                  <span className="service-row__detail">{service.detail}</span>
                  {!service.remediable && <span className="tag tag--muted">monitored only</span>}
                </div>
              ))
            )}
          </div>

          <h3>Known failure modes Sentinel watches for</h3>
          <div className="tag-list">
            {environment.application.known_failure_modes.map((mode) => (
              <span key={mode} className="tag">
                {titleCase(mode)}
              </span>
            ))}
          </div>
        </div>

        <div>
          <h3>Connectivity {testResult && <span className="muted small">(last checked just now)</span>}</h3>
          {testResult ? (
            <dl className="stat-list">
              <ConnectorRow name="Kubernetes" result={testResult.connectors.kubernetes} />
              <ConnectorRow name="Prometheus" result={testResult.connectors.prometheus} />
              <ConnectorRow name="Loki" result={testResult.connectors.loki} />
              <ConnectorRow name="GitHub" result={testResult.connectors.github} />
              <ConnectorRow name="AWS" result={testResult.connectors.aws} />
            </dl>
          ) : (
            <p className="muted">Run "Test connectivity" to probe each connector live.</p>
          )}

          <h3>Recent incidents</h3>
          {recentIncidents.length === 0 ? (
            <p className="muted">No incidents recorded for this environment yet.</p>
          ) : (
            <div className="incident-list">
              {recentIncidents.map((incident) => (
                <Link to={`/incidents/${incident.id}`} className="incident-row" key={incident.id}>
                  <span className={`severity-dot severity-dot--${incident.severity ?? 'unknown'}`} aria-hidden="true" />
                  <span className="incident-row__id">{incident.id}</span>
                  <span className="incident-row__app">{incident.app}</span>
                  <span className="incident-row__status">{incident.escalated ? 'Escalated' : titleCase(incident.status)}</span>
                  <span className="incident-row__time">{formatRelativeTime(incident.created_at)}</span>
                </Link>
              ))}
            </div>
          )}
        </div>
      </div>
    </section>
  )
}

export default function EnvironmentPage() {
  const { data: environmentsData, error: envError, loading: envLoading } = usePolling(listEnvironments, {
    intervalMs: 30000,
  })
  const { data: summary } = usePolling(getDashboardSummary, { intervalMs: 8000 })

  if (envLoading) return <Spinner label="Loading environment…" />
  if (envError) return <AlertBanner>{extractErrorMessage(envError, 'Could not load environments.')}</AlertBanner>

  const environments = environmentsData?.environments ?? []

  return (
    <div className="page">
      <div className="page__header">
        <h1>Environment</h1>
      </div>

      {environments.length === 0 ? (
        <p className="muted">No environment registered yet.</p>
      ) : (
        environments.map((environment) => (
          <EnvironmentCard
            key={environment.id}
            environment={environment}
            services={summary?.environment?.id === environment.id ? summary.system_health.services : []}
            recentIncidents={
              summary?.incidents.recent_incidents.filter((i) => i.environment_id === environment.id) ?? []
            }
          />
        ))
      )}
    </div>
  )
}
