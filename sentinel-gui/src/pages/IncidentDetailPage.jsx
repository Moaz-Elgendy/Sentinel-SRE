import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import AlertBanner from '../components/AlertBanner.jsx'
import Spinner from '../components/Spinner.jsx'
import AuditTimeline from '../components/incident/AuditTimeline.jsx'
import DecisionActionPanel from '../components/incident/DecisionActionPanel.jsx'
import EvidencePanel from '../components/incident/EvidencePanel.jsx'
import LiveFlowDiagram from '../components/incident/LiveFlowDiagram.jsx'
import ReasoningPanel from '../components/incident/ReasoningPanel.jsx'
import { extractErrorMessage } from '../api/client.js'
import { openIncidentDocument } from '../api/incidents.js'
import { getLifecyclePhases } from '../api/meta.js'
import { useLiveIncident } from '../hooks/useLiveIncident.js'
import { formatTimestamp, titleCase } from '../utils/format.js'

const TERMINAL_STATUSES = new Set(['resolved', 'escalated', 'auto_resolved'])

export default function IncidentDetailPage() {
  const { incidentId } = useParams()
  const [phaseMeta, setPhaseMeta] = useState(null)

  useEffect(() => {
    getLifecyclePhases().then(setPhaseMeta).catch(() => setPhaseMeta(null))
  }, [])

  // useLiveIncident (Phase B): instant re-fetch on a real SSE event for
  // this incident, plus a slow fallback poll — see hooks/useLiveIncident.js.
  const { incident, error, loading } = useLiveIncident(incidentId)

  if (loading) return <Spinner label="Loading incident…" />
  if (error) return <AlertBanner>{extractErrorMessage(error, 'Could not load this incident.')}</AlertBanner>
  if (!incident) return null

  const isTerminal = TERMINAL_STATUSES.has(incident.status)

  return (
    <div className="page">
      <div className="page__header">
        <div>
          <h1 className="mono">{incident.id}</h1>
          <p className="page__subtitle">
            {incident.app} · {incident.namespace} · {incident.alertname}
          </p>
        </div>
        <div className="incident-header__meta">
          <span className={`severity-dot severity-dot--${incident.severity}`} aria-hidden="true" />
          <span>{titleCase(incident.severity)}</span>
          <span className="muted">Started {formatTimestamp(incident.created_at)}</span>
          {!isTerminal && <span className="tag tag--live">LIVE</span>}
        </div>
      </div>

      <section className="card">
        <h2 className="card__title">Live Sentinel Operations</h2>
        <LiveFlowDiagram incident={incident} phaseMeta={phaseMeta} />
      </section>

      <div className="grid grid--2">
        <section className="card">
          <h2 className="card__title">Evidence</h2>
          <EvidencePanel evidence={incident.evidence} />
        </section>
        <section className="card">
          <h2 className="card__title">Diagnosis &amp; Reasoning</h2>
          <ReasoningPanel hypothesis={incident.hypothesis} />
        </section>
      </div>

      <section className="card">
        <h2 className="card__title">Decision &amp; Remediation</h2>
        <DecisionActionPanel attempts={incident.attempts} />
      </section>

      <section className="card">
        <div className="card__title-row">
          <h2 className="card__title">Audit Timeline</h2>
          {isTerminal && (
            <button type="button" className="link link--button" onClick={() => openIncidentDocument(incident.id)}>
              Open incident report ↗
            </button>
          )}
        </div>
        <AuditTimeline timeline={incident.timeline} />
      </section>
    </div>
  )
}
