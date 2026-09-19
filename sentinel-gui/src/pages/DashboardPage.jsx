import { Link } from 'react-router-dom'
import { extractErrorMessage } from '../api/client.js'
import AlertBanner from '../components/ui/AlertBanner.jsx'
import Card from '../components/ui/Card.jsx'
import EmptyState, { ErrorState } from '../components/ui/EmptyState.jsx'
import Icon from '../components/ui/Icon.jsx'
import LastUpdated from '../components/ui/LastUpdated.jsx'
import { DashboardSkeleton } from '../components/ui/Loading.jsx'
import PageHeader from '../components/ui/PageHeader.jsx'
import SeverityBadge from '../components/ui/SeverityBadge.jsx'
import StatusPill, { StatusDot } from '../components/ui/StatusPill.jsx'
import Tag from '../components/ui/Tag.jsx'
import Timestamp from '../components/ui/Timestamp.jsx'
import { useSummary } from '../context/SummaryContext.jsx'
import { usePageTitle } from '../hooks/usePageTitle.js'
import { titleCase } from '../utils/format.js'
import { effectiveIncidentStatus, HEALTH_HEADLINE, statusLabel, statusTone } from '../utils/status.js'

function serviceStatus(service) {
  return service.healthy === true ? 'healthy' : service.healthy === false ? 'down' : 'unknown'
}

function ServiceRow({ service }) {
  const status = serviceStatus(service)
  return (
    <li className="service-row">
      <StatusDot status={status} label={statusLabel(service.healthy)} />
      <span className="service-row__name">{service.name}</span>
      <span className="service-row__detail truncate" title={service.detail}>
        {service.detail}
      </span>
      {!service.remediable && (
        <Tag title="Sentinel observes this service but does not remediate it">Monitored only</Tag>
      )}
      <span className={`service-row__state service-row__state--${statusTone(status)}`}>{statusLabel(service.healthy)}</span>
    </li>
  )
}

function IncidentRow({ incident }) {
  return (
    <li>
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
  )
}

function Kpi({ scope, label, value, tone }) {
  return (
    <div className="kpi">
      <span className="kpi__scope">{scope}</span>
      <span className={`kpi__value${tone ? ` kpi__value--${tone}` : ''}`}>{value}</span>
      <span className="kpi__label">{label}</span>
    </div>
  )
}

export default function DashboardPage() {
  usePageTitle('Dashboard')
  const { data: summary, error, loading, updatedAt, busy, refetch } = useSummary()

  if (loading && !summary) return <DashboardSkeleton />
  if (!summary) {
    return (
      <ErrorState
        title="Couldn't load the dashboard"
        message={extractErrorMessage(error, 'Sentinel did not respond.')}
        onRetry={refetch}
      />
    )
  }

  const { sentinel, environment, system_health: systemHealth, incidents, latest_config_change: latestConfigChange } =
    summary
  const services = systemHealth.services
  const healthyCount = services.filter((s) => s.healthy === true).length
  const unhealthyCount = services.filter((s) => s.healthy === false).length
  const healthTone = statusTone(systemHealth.status)

  let healthDetail = `${healthyCount} of ${services.length} services healthy`
  if (unhealthyCount > 0) healthDetail = `${unhealthyCount} of ${services.length} services unhealthy`
  if (services.length === 0) healthDetail = 'No services registered'

  return (
    <div className="page">
      <PageHeader
        title="System health"
        subtitle={`${environment.name} · ${environment.customer_id}`}
        actions={<LastUpdated updatedAt={updatedAt} stale={Boolean(error)} busy={busy} />}
      />

      {error && (
        <AlertBanner tone="warn" title="Showing the last data received">
          {extractErrorMessage(error, 'Sentinel did not respond to the latest refresh.')} Retrying automatically.
        </AlertBanner>
      )}

      {latestConfigChange && (
        <Link to="/config-history" className="alert alert--info">
          <Icon name="info" size={16} className="alert__icon" />
          <span className="alert__body">
            {titleCase(latestConfigChange.category)} configuration changed{' '}
            <Timestamp epoch={latestConfigChange.changed_at} /> by {latestConfigChange.changed_by}
          </span>
          <span className="alert__action link">
            View history <Icon name="arrowRight" size={12} />
          </span>
        </Link>
      )}

      <section className={`card hero hero--${healthTone}`} aria-label="Overall status">
        <div className="hero__top">
          <div className="hero__state">
            <StatusPill
              size="lg"
              status={systemHealth.status}
              label={HEALTH_HEADLINE[systemHealth.status] ?? HEALTH_HEADLINE.unknown}
            />
            <span className="hero__detail">{healthDetail}</span>
          </div>
          <Link to="/incidents" className="link">
            View incidents <Icon name="arrowRight" size={12} />
          </Link>
        </div>
        <div className="hero__kpis">
          <Kpi
            scope="Now"
            label="Active incidents"
            value={incidents.active_incidents}
            tone={incidents.active_incidents > 0 ? 'info' : undefined}
          />
          <Kpi scope="All time" label="Autonomous resolutions" value={incidents.autonomous_resolutions} />
          <Kpi scope="All time" label="Escalated" value={incidents.escalated_incidents} />
          <Kpi scope="All time" label="Total recorded" value={incidents.total_incidents} />
        </div>
      </section>

      <div className="grid dashboard-split">
        <Card title="Services" description="Current health of each monitored service." flush>
          {services.length === 0 ? (
            <EmptyState compact title="No services registered" description="Services appear here once the environment lists them." />
          ) : (
            <ul className="service-list">
              {services.map((service) => (
                <ServiceRow key={service.name} service={service} />
              ))}
            </ul>
          )}
        </Card>

        <Card title="Sentinel" description="The autonomous agent's own state.">
          <dl>
            <div className="dl__row">
              <dt>Monitoring</dt>
              <dd>
                <StatusPill
                  status={sentinel.monitoring ? 'operational' : 'critical'}
                  label={sentinel.monitoring ? 'Active' : 'Inactive'}
                />
              </dd>
            </div>
            <div className="dl__row">
              <dt>Mode</dt>
              <dd>{titleCase(sentinel.mode)}</dd>
            </div>
            <div className="dl__row">
              <dt>Reasoning</dt>
              <dd>{sentinel.llm === 'enabled' ? 'LLM + rules' : 'Rule-based only'}</dd>
            </div>
            <div className="dl__row">
              <dt>Kubernetes</dt>
              <dd>
                <StatusPill
                  status={sentinel.kubernetes_available ? 'operational' : 'critical'}
                  label={sentinel.kubernetes_available ? 'Reachable' : 'Unreachable'}
                />
              </dd>
            </div>
            <div className="dl__row">
              <dt>Last incident</dt>
              <dd>
                <Timestamp epoch={incidents.last_incident_at} />
              </dd>
            </div>
          </dl>
        </Card>
      </div>

      <Card
        title="Recent incidents"
        flush
        actions={
          <Link to="/incidents" className="link">
            View all <Icon name="arrowRight" size={12} />
          </Link>
        }
      >
        {incidents.recent_incidents.length === 0 ? (
          <EmptyState
            compact
            title="No incidents recorded yet"
            description="When Sentinel detects and handles an incident, it shows up here."
          />
        ) : (
          <ul className="incident-list">
            {incidents.recent_incidents.map((incident) => (
              <IncidentRow key={incident.id} incident={incident} />
            ))}
          </ul>
        )}
      </Card>
    </div>
  )
}
