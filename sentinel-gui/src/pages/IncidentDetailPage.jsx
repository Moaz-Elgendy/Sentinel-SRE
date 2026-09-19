import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { extractErrorMessage } from '../api/client.js'
import { listIncidentFeedback, submitDiagnosisFeedback, submitRemediationFeedback } from '../api/feedback.js'
import { openIncidentDocument } from '../api/incidents.js'
import { getLifecyclePhases, listActionTypes, listRootCauses } from '../api/meta.js'
import AuditTimeline from '../components/incident/AuditTimeline.jsx'
import AuthorizationPanel from '../components/incident/AuthorizationPanel.jsx'
import DecisionActionPanel from '../components/incident/DecisionActionPanel.jsx'
import EvidencePanel from '../components/incident/EvidencePanel.jsx'
import FeedbackForm from '../components/incident/FeedbackForm.jsx'
import LiveFlowDiagram from '../components/incident/LiveFlowDiagram.jsx'
import ReasoningPanel from '../components/incident/ReasoningPanel.jsx'
import AlertBanner from '../components/ui/AlertBanner.jsx'
import Button from '../components/ui/Button.jsx'
import Card from '../components/ui/Card.jsx'
import CopyButton from '../components/ui/CopyButton.jsx'
import EmptyState from '../components/ui/EmptyState.jsx'
import { DetailSkeleton } from '../components/ui/Loading.jsx'
import LiveBadge from '../components/ui/LiveBadge.jsx'
import PageHeader from '../components/ui/PageHeader.jsx'
import SeverityBadge from '../components/ui/SeverityBadge.jsx'
import StatusPill from '../components/ui/StatusPill.jsx'
import Timestamp from '../components/ui/Timestamp.jsx'
import { useToast } from '../components/ui/Toast.jsx'
import { useLiveIncident } from '../hooks/useLiveIncident.js'
import { usePageTitle } from '../hooks/usePageTitle.js'
import { formatDuration, formatPercent, formatTimestamp, titleCase } from '../utils/format.js'
import { effectiveIncidentStatus } from '../utils/status.js'

const TERMINAL_STATUSES = new Set(['resolved', 'escalated', 'auto_resolved'])

function SummaryItem({ label, children }) {
  return (
    <div className="summary__item">
      <div className="eyebrow">{label}</div>
      <div className="summary__value">{children}</div>
    </div>
  )
}

function outcomeText(incident) {
  if (incident.status === 'escalated') return 'Waiting for an SRE'
  if ((incident.status === 'resolved' || incident.status === 'auto_resolved') && incident.resolved_at != null) {
    return `Recovered in ${formatDuration(incident.resolved_at - incident.created_at)}`
  }
  if (TERMINAL_STATUSES.has(incident.status)) return titleCase(incident.status)
  return `Phase: ${titleCase(incident.phase)}`
}

export default function IncidentDetailPage() {
  const { incidentId } = useParams()
  usePageTitle(incidentId)
  const toast = useToast()
  const [phaseMeta, setPhaseMeta] = useState(null)
  const [rootCauses, setRootCauses] = useState([])
  const [actionTypes, setActionTypes] = useState([])
  const [feedback, setFeedback] = useState([])
  const [openingReport, setOpeningReport] = useState(false)

  useEffect(() => {
    getLifecyclePhases()
      .then(setPhaseMeta)
      .catch(() => setPhaseMeta(null))
    listRootCauses()
      .then(setRootCauses)
      .catch(() => setRootCauses([]))
    listActionTypes()
      .then(setActionTypes)
      .catch(() => setActionTypes([]))
  }, [])

  const refreshFeedback = useCallback(() => {
    listIncidentFeedback(incidentId)
      .then(setFeedback)
      .catch(() => setFeedback([]))
  }, [incidentId])

  useEffect(() => {
    refreshFeedback()
  }, [refreshFeedback])

  // useLiveIncident (Phase B): instant re-fetch on a real SSE event for
  // this incident, plus a slow fallback poll — see hooks/useLiveIncident.js.
  const { incident, error, loading } = useLiveIncident(incidentId)

  async function handleOpenReport() {
    setOpeningReport(true)
    try {
      await openIncidentDocument(incidentId)
    } catch (err) {
      toast(extractErrorMessage(err, 'Could not open the incident report.'), { tone: 'error' })
    } finally {
      setOpeningReport(false)
    }
  }

  if (loading && !incident) return <DetailSkeleton label="Loading incident…" />

  if (!incident) {
    return (
      <div className="page">
        <PageHeader title={<span className="mono">{incidentId}</span>} breadcrumb={[{ to: '/incidents', label: 'Incidents' }]} />
        <Card>
          <EmptyState
            icon="alertCircle"
            title="Couldn't load this incident"
            description={extractErrorMessage(error, 'It may not exist, or Sentinel may be unreachable.')}
            action={
              <Link to="/incidents" className="button">
                Back to incidents
              </Link>
            }
          />
        </Card>
      </div>
    )
  }

  const isTerminal = TERMINAL_STATUSES.has(incident.status)
  const hypothesis = incident.hypothesis
  const diagnosisFeedback = feedback.filter((f) => f.kind === 'diagnosis')
  const remediationFeedback = feedback.filter((f) => f.kind === 'remediation')
  const showFeedback = Boolean(hypothesis) || incident.attempts?.length > 0

  return (
    <div className="page">
      <PageHeader
        breadcrumb={[{ to: '/incidents', label: 'Incidents' }]}
        title={<span className="mono">{incident.id}</span>}
        titleExtra={
          <>
            <CopyButton value={incident.id} label="Copy incident ID" />
            <StatusPill size="lg" status={effectiveIncidentStatus(incident)} />
            <SeverityBadge severity={incident.severity} />
            {!isTerminal && <LiveBadge />}
          </>
        }
        subtitle={
          <>
            {incident.app} · {incident.namespace} · <span className="mono">{incident.alertname}</span>
          </>
        }
        actions={
          isTerminal && (
            <Button icon="externalLink" onClick={handleOpenReport} busy={openingReport} busyLabel="Opening…">
              Open incident report
            </Button>
          )
        }
      />

      {error && (
        <AlertBanner tone="warn" title="Showing the last data received">
          {extractErrorMessage(error, 'Could not refresh this incident.')} Retrying automatically.
        </AlertBanner>
      )}

      {/* What happened, at a glance — every value here comes from the record. */}
      <section className="card summary" aria-label="Incident summary">
        <SummaryItem label="Outcome">{outcomeText(incident)}</SummaryItem>
        <SummaryItem label="Started">
          <Timestamp epoch={incident.created_at} />
          <span className="summary__sub">{formatTimestamp(incident.created_at)}</span>
        </SummaryItem>
        <SummaryItem label="Diagnosis">
          {hypothesis ? (
            <>
              {titleCase(hypothesis.root_cause)}
              <span className="summary__sub">{formatPercent(hypothesis.confidence)} confidence</span>
            </>
          ) : (
            <span className="muted">Pending</span>
          )}
        </SummaryItem>
        <SummaryItem label="Recommended action">
          {hypothesis ? titleCase(hypothesis.recommended_action) : <span className="muted">Pending</span>}
        </SummaryItem>
      </section>

      {/* When Sentinel has escalated, the one thing that needs a human comes first. */}
      {incident.status === 'escalated' && (
        <Card
          tone="warn"
          title="Action required — temporary human authorization"
          description="Sentinel couldn't act safely on its own. You can authorize one action for this incident."
        >
          <AuthorizationPanel incident={incident} actionTypes={actionTypes} />
        </Card>
      )}

      <div className="incident-layout">
        <aside className="incident-layout__rail" aria-label="Sentinel operations">
          <Card title="Sentinel operations">
            <LiveFlowDiagram incident={incident} phaseMeta={phaseMeta} />
          </Card>
        </aside>

        <div className="incident-layout__main stack">
          <Card title="Diagnosis & reasoning" description="Sentinel's interpretation of the evidence.">
            <ReasoningPanel hypothesis={hypothesis} />
          </Card>

          <Card title="Decision & remediation" description="What Sentinel planned, whether policy allowed it, and what happened.">
            <DecisionActionPanel attempts={incident.attempts} />
          </Card>

          <Card title="Evidence" description="Facts collected from the system — no interpretation.">
            <EvidencePanel evidence={incident.evidence} />
          </Card>

          {showFeedback && (
            <Card
              title="Feedback"
              description="Records how accurate Sentinel was. Submitting feedback does not change Sentinel's behavior."
            >
              <div className="stack stack--lg">
                {hypothesis && (
                  <FeedbackForm
                    question="Was the diagnosis correct?"
                    correctionLabel="What was the actual root cause?"
                    options={rootCauses}
                    history={diagnosisFeedback}
                    onSubmit={({ answer, correction, note }) =>
                      submitDiagnosisFeedback(incident.id, { correct: answer, actualRootCause: correction, note }).then(
                        refreshFeedback
                      )
                    }
                  />
                )}
                {hypothesis && incident.attempts?.length > 0 && <hr className="divider" />}
                {incident.attempts?.length > 0 && (
                  <FeedbackForm
                    question="Was the remediation useful?"
                    correctionLabel="What should Sentinel have done instead?"
                    options={actionTypes}
                    history={remediationFeedback}
                    onSubmit={({ answer, correction, note }) =>
                      submitRemediationFeedback(incident.id, { useful: answer, suggestedAction: correction, note }).then(
                        refreshFeedback
                      )
                    }
                  />
                )}
              </div>
            </Card>
          )}

          <Card title="Audit timeline" description="Every recorded event, oldest first.">
            <AuditTimeline timeline={incident.timeline} />
          </Card>
        </div>
      </div>
    </div>
  )
}
