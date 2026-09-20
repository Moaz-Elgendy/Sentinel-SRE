import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { extractErrorMessage } from '../api/client.js'
import { getLifecyclePhases } from '../api/meta.js'
import AlertBanner from '../components/ui/AlertBanner.jsx'
import Card from '../components/ui/Card.jsx'
import { ErrorState } from '../components/ui/EmptyState.jsx'
import Icon from '../components/ui/Icon.jsx'
import LastUpdated from '../components/ui/LastUpdated.jsx'
import { PageSkeleton } from '../components/ui/Loading.jsx'
import PageHeader from '../components/ui/PageHeader.jsx'
import { StatusDot } from '../components/ui/StatusPill.jsx'
import Timestamp from '../components/ui/Timestamp.jsx'
import { useSentinelActivity } from '../hooks/useSentinelActivity.js'
import { statusLabel } from '../utils/status.js'
import { formatClock, titleCase } from '../utils/format.js'

// Presentation only — which emoji represents each REAL lifecycle phase
// (see app/models/incident.py's LifecyclePhase). The phase and message
// themselves always come from a genuine backend event; this map never
// invents state, only decorates it. See useSentinelActivity.js.
const PHASE_ICON = {
  detection: '🚨',
  investigation: '🔍',
  correlation: '🔗',
  root_cause_analysis: '🧠',
  remediation_decision: '📊',
  policy_check: '🛡️',
  autonomous_execution: '🔧',
  recovery_validation: '⏳',
  re_investigation: '🔁',
  documentation: '📝',
  notification: '📣',
  learning: '🎓',
  escalation: '🆘',
}

function ActivityStatusHeadline({ status }) {
  if (status.state === 'investigating') {
    return (
      <div className="sentinel-live__headline">
        <span className="sentinel-live__headline-icon" aria-hidden="true">
          🚨
        </span>
        <div>
          <div className="sentinel-live__headline-title">
            Investigating{' '}
            <Link to={`/incidents/${status.incident_id}`} className="link">
              {status.incident_id}
            </Link>
          </div>
          <div className="sentinel-live__headline-sub">{status.alertname}</div>
        </div>
      </div>
    )
  }

  const icon = status.state === 'monitoring' ? '🟢' : '🟠'
  return (
    <div className="sentinel-live__headline">
      <span className="sentinel-live__headline-icon" aria-hidden="true">
        {icon}
      </span>
      <div>
        <div className="sentinel-live__headline-title">{statusLabel(status.state)}</div>
        <div className="sentinel-live__headline-sub">{status.message}</div>
      </div>
    </div>
  )
}

function WatcherRow({ watcher }) {
  return (
    <li className="watcher-row">
      <StatusDot
        status={watcher.connected ? 'healthy' : 'critical'}
        label={watcher.connected ? 'Connected' : 'Unavailable'}
      />
      <span className="watcher-row__name">{watcher.name}</span>
      <span className={`watcher-row__state${watcher.connected ? '' : ' watcher-row__state--down'}`}>
        {watcher.connected ? 'Connected' : 'Unavailable'}
      </span>
    </li>
  )
}

function FeedRow({ entry, phaseLabels }) {
  const label = phaseLabels?.[entry.phase] ?? titleCase(entry.phase)
  return (
    <li className="activity-feed__row">
      <time className="activity-feed__time mono">{formatClock(entry.at)}</time>
      <span className="activity-feed__icon" aria-hidden="true">
        {PHASE_ICON[entry.phase] ?? '•'}
      </span>
      <span className="activity-feed__body">
        <span className="activity-feed__phase">{label}</span>
        <span className="activity-feed__message">{entry.message}</span>
      </span>
    </li>
  )
}

export default function SentinelLivePage() {
  const { status, feed, error, loading, busy, updatedAt, refetch } = useSentinelActivity()
  const [phaseMeta, setPhaseMeta] = useState(null)

  useEffect(() => {
    getLifecyclePhases()
      .then(setPhaseMeta)
      .catch(() => setPhaseMeta(null))
  }, [])

  if (loading && !status) return <PageSkeleton label="Loading Sentinel Live…" cards={2} />
  if (!status) {
    return (
      <ErrorState
        title="Couldn't load Sentinel's current status"
        message={extractErrorMessage(error, 'Sentinel did not respond.')}
        onRetry={refetch}
      />
    )
  }

  const isInvestigating = status.state === 'investigating'

  return (
    <div className="page">
      <PageHeader
        title="Sentinel Live"
        subtitle="What Sentinel is doing right now — separate from any one incident's own detail page."
        actions={<LastUpdated updatedAt={updatedAt} stale={Boolean(error)} busy={busy} />}
      />

      {error && (
        <AlertBanner tone="warn" title="Showing the last status received">
          {extractErrorMessage(error, 'Could not refresh Sentinel Live.')} Retrying automatically.
        </AlertBanner>
      )}

      <Card tone={status.state === 'degraded' ? 'warn' : undefined} className="sentinel-live__status">
        <ActivityStatusHeadline status={status} />
      </Card>

      {!isInvestigating && (
        <Card title="Watching" description="Real connectivity, checked live — never fabricated.">
          <ul className="watcher-grid">
            {(status.watchers ?? []).map((watcher) => (
              <WatcherRow key={watcher.name} watcher={watcher} />
            ))}
          </ul>
          {status.last_activity != null && (
            <p className="sentinel-live__last-activity">
              Last activity <Timestamp epoch={status.last_activity} />
            </p>
          )}
        </Card>
      )}

      <Card
        title={isInvestigating ? 'Live activity' : 'Recent activity'}
        description={
          isInvestigating
            ? 'Real lifecycle events from the incident above, as they happen.'
            : 'Nothing happening right now. This fills in the moment Sentinel starts investigating.'
        }
        flush
      >
        {feed.length === 0 ? (
          <div className="empty empty--compact">
            <div className="empty__icon">
              <Icon name="radio" size={18} />
            </div>
            <div className="empty__title">No activity yet</div>
            <p className="empty__description">This feed fills in the moment Sentinel starts investigating something.</p>
          </div>
        ) : (
          <ul className="activity-feed">
            {feed.map((entry, index) => (
              // eslint-disable-next-line react/no-array-index-key
              <FeedRow key={`${entry.incident_id}-${entry.phase}-${index}`} entry={entry} phaseLabels={phaseMeta?.labels} />
            ))}
          </ul>
        )}
      </Card>
    </div>
  )
}
