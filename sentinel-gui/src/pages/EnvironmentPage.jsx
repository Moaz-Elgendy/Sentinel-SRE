import { useState } from 'react'
import { Link } from 'react-router-dom'
import { extractErrorMessage } from '../api/client.js'
import { listEnvironments, testEnvironmentConnection } from '../api/environments.js'
import AlertBanner from '../components/ui/AlertBanner.jsx'
import Button from '../components/ui/Button.jsx'
import Card from '../components/ui/Card.jsx'
import EmptyState, { ErrorState } from '../components/ui/EmptyState.jsx'
import { PageSkeleton } from '../components/ui/Loading.jsx'
import PageHeader from '../components/ui/PageHeader.jsx'
import SeverityBadge from '../components/ui/SeverityBadge.jsx'
import StatusPill, { StatusDot } from '../components/ui/StatusPill.jsx'
import Tag from '../components/ui/Tag.jsx'
import Timestamp from '../components/ui/Timestamp.jsx'
import { useSummary } from '../context/SummaryContext.jsx'
import { usePageTitle } from '../hooks/usePageTitle.js'
import { usePolling } from '../hooks/usePolling.js'
import { titleCase } from '../utils/format.js'
import { effectiveIncidentStatus, statusLabel } from '../utils/status.js'

const CONNECTORS = [
  { key: 'kubernetes', name: 'Kubernetes' },
  { key: 'prometheus', name: 'Prometheus' },
  { key: 'loki', name: 'Loki' },
  { key: 'github', name: 'GitHub' },
  { key: 'aws', name: 'AWS' },
]

function ConnectorRow({ name, result }) {
  if (!result) return null
  const status = result.ok === null ? 'unknown' : result.ok ? 'operational' : 'critical'
  const label = result.ok === null ? 'Not configured' : result.ok ? 'Reachable' : 'Unreachable'
  return (
    <div className="dl__row connector-row">
      <dt>{name}</dt>
      <dd>
        <StatusPill status={status} label={label} />
        {result.detail && <div className="muted small connector-row__detail">{result.detail}</div>}
      </dd>
    </div>
  )
}

function EnvironmentCard({ environment, services, recentIncidents }) {
  const [testResult, setTestResult] = useState(null)
  const [testedAt, setTestedAt] = useState(null)
  const [testing, setTesting] = useState(false)
  const [testError, setTestError] = useState(null)

  async function handleTestConnection() {
    setTesting(true)
    setTestError(null)
    try {
      const result = await testEnvironmentConnection(environment.id)
      setTestResult(result)
      setTestedAt(Date.now() / 1000)
    } catch (err) {
      setTestError(extractErrorMessage(err, 'Could not run the connectivity test.'))
    } finally {
      setTesting(false)
    }
  }

  return (
    <Card
      title={
        <>
          {environment.customer_id} <span className="faint">/</span> {environment.name}
        </>
      }
      description={`Kubernetes: ${environment.kubernetes.mode} · namespace ${environment.kubernetes.namespace}`}
      actions={
        <Button icon="refresh" onClick={handleTestConnection} busy={testing} busyLabel="Testing…">
          Test connectivity
        </Button>
      }
    >
      <div className="stack">
        {testError && <AlertBanner>{testError}</AlertBanner>}

        <div className="grid grid--2">
          <div className="stack">
            <div>
              <h3 className="section-label">Services</h3>
              {services.length === 0 ? (
                <p className="muted">No services configured for this environment.</p>
              ) : (
                <ul className="service-list service-list--bordered">
                  {services.map((service) => (
                    <li className="service-row" key={service.name}>
                      <StatusDot
                        status={service.healthy === true ? 'healthy' : service.healthy === false ? 'down' : 'unknown'}
                        label={statusLabel(service.healthy)}
                      />
                      <span className="service-row__name">{service.name}</span>
                      <span className="service-row__detail truncate" title={service.detail}>
                        {service.detail}
                      </span>
                      {!service.remediable && <Tag>Monitored only</Tag>}
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <div>
              <h3 className="section-label">Failure modes Sentinel watches for</h3>
              <div className="tag-list">
                {environment.application.known_failure_modes.map((mode) => (
                  <Tag key={mode}>{titleCase(mode)}</Tag>
                ))}
              </div>
            </div>
          </div>

          <div className="stack">
            <div>
              <h3 className="section-label">
                Connectivity{' '}
                {testedAt && (
                  <span className="faint">
                    · checked <Timestamp epoch={testedAt} />
                  </span>
                )}
              </h3>
              {testResult ? (
                <dl>
                  {CONNECTORS.map(({ key, name }) => (
                    <ConnectorRow key={key} name={name} result={testResult.connectors[key]} />
                  ))}
                </dl>
              ) : (
                <p className="muted">
                  Run “Test connectivity” to probe Kubernetes, Prometheus, Loki, GitHub and AWS live. Nothing is cached — each
                  run is a real check.
                </p>
              )}
            </div>

            <div>
              <h3 className="section-label">Recent incidents</h3>
              {recentIncidents.length === 0 ? (
                <p className="muted">No incidents recorded for this environment yet.</p>
              ) : (
                <ul className="incident-list incident-list--bordered">
                  {recentIncidents.map((incident) => (
                    <li key={incident.id}>
                      <Link to={`/incidents/${incident.id}`} className="incident-row">
                        <span className="incident-row__id">
                          <SeverityBadge severity={incident.severity} />
                          <span className="mono">{incident.id}</span>
                        </span>
                        <span className="incident-row__app truncate">{incident.app}</span>
                        <StatusPill status={effectiveIncidentStatus(incident)} />
                        <Timestamp epoch={incident.created_at} className="incident-row__time" />
                      </Link>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </div>
      </div>
    </Card>
  )
}

export default function EnvironmentPage() {
  usePageTitle('Environment')
  const {
    data: environmentsData,
    error: envError,
    loading: envLoading,
    refetch,
  } = usePolling(listEnvironments, { intervalMs: 30000 })
  const { data: summary } = useSummary()

  if (envLoading && !environmentsData) return <PageSkeleton label="Loading environment…" cards={1} />
  if (!environmentsData) {
    return (
      <ErrorState
        title="Couldn't load environments"
        message={extractErrorMessage(envError, 'Sentinel did not respond.')}
        onRetry={refetch}
      />
    )
  }

  const environments = environmentsData?.environments ?? []

  return (
    <div className="page">
      <PageHeader title="Environment" subtitle="What Sentinel is watching, and whether it can reach everything it needs." />

      {envError && (
        <AlertBanner tone="warn" title="Showing the last data received">
          {extractErrorMessage(envError, 'Could not refresh environments.')} Retrying automatically.
        </AlertBanner>
      )}

      {environments.length === 0 ? (
        <Card>
          <EmptyState
            icon="server"
            title="No environment registered yet"
            description="Sentinel needs a registered environment before it can monitor or remediate anything."
          />
        </Card>
      ) : (
        environments.map((environment) => (
          <EnvironmentCard
            key={environment.id}
            environment={environment}
            services={summary?.environment?.id === environment.id ? summary.system_health.services : []}
            recentIncidents={summary?.incidents.recent_incidents.filter((i) => i.environment_id === environment.id) ?? []}
          />
        ))
      )}
    </div>
  )
}
