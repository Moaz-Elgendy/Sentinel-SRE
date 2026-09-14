import { Link } from 'react-router-dom'
import AlertBanner from '../components/AlertBanner.jsx'
import Spinner from '../components/Spinner.jsx'
import StatusPill from '../components/StatusPill.jsx'
import { getDashboardSummary } from '../api/dashboard.js'
import { extractErrorMessage } from '../api/client.js'
import { usePolling } from '../hooks/usePolling.js'
import { formatRelativeTime, titleCase } from '../utils/format.js'

const SERVICE_STATUS_ICON = { true: '🟢', false: '🔴', null: '⚪' }

function ServiceRow({ service }) {
  return (
    <div className="service-row">
      <span className="service-row__icon" aria-hidden="true">
        {SERVICE_STATUS_ICON[String(service.healthy)]}
      </span>
      <span className="service-row__name">{service.name}</span>
      <span className="service-row__detail">{service.detail}</span>
      {!service.remediable && <span className="tag tag--muted">monitored only</span>}
    </div>
  )
}

function IncidentRow({ incident }) {
  return (
    <Link to={`/incidents/${incident.id}`} className="incident-row">
      <span className={`severity-dot severity-dot--${incident.severity ?? 'unknown'}`} aria-hidden="true" />
      <span className="incident-row__id">{incident.id}</span>
      <span className="incident-row__app">{incident.app}</span>
      <span className="incident-row__status">
        {incident.escalated ? 'Escalated' : titleCase(incident.status)}
      </span>
      <span className="incident-row__time">{formatRelativeTime(incident.created_at)}</span>
    </Link>
  )
}

export default function DashboardPage() {
  const { data: summary, error, loading } = usePolling(getDashboardSummary, { intervalMs: 8000 })

  if (loading) return <Spinner label="Loading Sentinel status…" />
  if (error) return <AlertBanner>{extractErrorMessage(error, 'Could not load the dashboard.')}</AlertBanner>
  if (!summary) return null

  const { sentinel, environment, system_health: systemHealth, incidents } = summary

  return (
    <div className="page">
      <div className="page__header">
        <div>
          <h1>System Health</h1>
          <p className="page__subtitle">
            {environment.name} · {environment.customer_id}
          </p>
        </div>
        <StatusPill
          status={systemHealth.status}
          label={
            systemHealth.status === 'operational'
              ? 'All Systems Operational'
              : systemHealth.status === 'degraded'
                ? 'Degraded'
                : 'Status Unknown'
          }
        />
      </div>

      <div className="grid grid--3">
        <section className="card">
          <h2 className="card__title">Services</h2>
          <div className="service-list">
            {systemHealth.services.map((service) => (
              <ServiceRow key={service.name} service={service} />
            ))}
          </div>
        </section>

        <section className="card">
          <h2 className="card__title">Sentinel</h2>
          <dl className="stat-list">
            <div className="stat-list__row">
              <dt>Monitoring</dt>
              <dd><StatusPill status={sentinel.monitoring ? 'operational' : 'critical'} label={sentinel.monitoring ? 'Active' : 'Inactive'} /></dd>
            </div>
            <div className="stat-list__row">
              <dt>Mode</dt>
              <dd>{titleCase(sentinel.mode)}</dd>
            </div>
            <div className="stat-list__row">
              <dt>Reasoning</dt>
              <dd>{sentinel.llm === 'enabled' ? 'LLM + rules' : 'Rule-based only'}</dd>
            </div>
            <div className="stat-list__row">
              <dt>Kubernetes</dt>
              <dd>
                <StatusPill
                  status={sentinel.kubernetes_available ? 'operational' : 'critical'}
                  label={sentinel.kubernetes_available ? 'Reachable' : 'Unreachable'}
                />
              </dd>
            </div>
            <div className="stat-list__row">
              <dt>Last incident</dt>
              <dd>{formatRelativeTime(incidents.last_incident_at)}</dd>
            </div>
          </dl>
        </section>

        <section className="card">
          <h2 className="card__title">Incident Counts</h2>
          <div className="kpi-grid">
            <div className="kpi">
              <div className="kpi__value">{incidents.active_incidents}</div>
              <div className="kpi__label">Active</div>
            </div>
            <div className="kpi">
              <div className="kpi__value">{incidents.autonomous_resolutions}</div>
              <div className="kpi__label">Autonomous resolutions</div>
            </div>
            <div className="kpi">
              <div className="kpi__value">{incidents.escalated_incidents}</div>
              <div className="kpi__label">Escalated</div>
            </div>
            <div className="kpi">
              <div className="kpi__value">{incidents.total_incidents}</div>
              <div className="kpi__label">Total recorded</div>
            </div>
          </div>
        </section>
      </div>

      <section className="card">
        <div className="card__title-row">
          <h2 className="card__title">Recent Incidents</h2>
          <Link to="/incidents" className="link">View all →</Link>
        </div>
        {incidents.recent_incidents.length === 0 ? (
          <p className="muted">No incidents recorded yet.</p>
        ) : (
          <div className="incident-list">
            {incidents.recent_incidents.map((incident) => (
              <IncidentRow key={incident.id} incident={incident} />
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
