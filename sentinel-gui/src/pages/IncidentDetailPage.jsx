import { FileText, LoaderCircle, SearchX } from 'lucide-react'
import { useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { extractErrorMessage } from '@/api/client'
import { openIncidentDocument } from '@/api/incidents'
import { AttentionPanel } from '@/components/incident/AttentionPanel'
import { DecisionSection } from '@/components/incident/DecisionSection'
import { DecisionSummaryCard } from '@/components/incident/DecisionSummaryCard'
import { DiagnosisSection } from '@/components/incident/DiagnosisSection'
import { CausalGraphTab } from '@/components/incident/CausalGraphTab'
import { ReplayTab } from '@/components/incident/ReplayTab'
import { EvidenceTab } from '@/components/incident/EvidenceTab'
import { FactsPanel } from '@/components/incident/FactsPanel'
import { FeedbackTab } from '@/components/incident/FeedbackTab'
import { ObservedSection } from '@/components/incident/ObservedSection'
import { TimelineTab } from '@/components/incident/TimelineTab'
import { LogViewer } from '@/components/logs/LogViewer'
import { CopyButton } from '@/components/sentinel/CopyButton'
import { LifecycleRail } from '@/components/sentinel/LifecycleRail'
import { Panel } from '@/components/sentinel/Panel'
import { EmptyState, ErrorState, SkeletonRows } from '@/components/sentinel/States'
import { SeverityBadge } from '@/components/sentinel/SeverityBadge'
import { LiveDot, StatusBadge } from '@/components/sentinel/StatusBadge'
import { Timestamp } from '@/components/sentinel/Timestamp'
import { TONE_SURFACE, TONE_TEXT } from '@/components/sentinel/tone'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useLiveIncident } from '@/hooks/useLiveIncident'
import { useNow } from '@/hooks/useNow'
import { usePageTitle } from '@/hooks/usePageTitle'
import { useRcaThresholds } from '@/hooks/useRcaThresholds'
import { notify } from '@/lib/notify'
import { cn } from '@/lib/utils'
import { formatSpan } from '@/utils/format'
import { durationLabel, incidentDuration, incidentHeadline, incidentOutcome, incidentStatus, isActiveIncident, isAwaitingHuman } from '@/utils/incident'

const TABS = ['investigation', 'evidence', 'graph', 'replay', 'timeline', 'logs', 'feedback']

function OutcomeStrip({ incident, now }) {
  const outcome = incidentOutcome(incident, now / 1000)
  if (outcome.kind === 'awaiting') return null // the attention panel owns this state
  return (
    <div className={cn('mt-4 flex flex-wrap items-baseline gap-x-3 gap-y-1 rounded-md border px-3.5 py-2.5', TONE_SURFACE[outcome.tone])}>
      <p className={cn('text-sm font-semibold', TONE_TEXT[outcome.tone])}>{outcome.title}</p>
      {outcome.kind !== 'in_progress' && outcome.duration != null && <p className="tnum text-sm">in {formatSpan(outcome.duration)}</p>}
      {outcome.detail && <p className="min-w-0 text-sm text-muted-foreground">{outcome.detail}</p>}
    </div>
  )
}

export default function IncidentDetailPage() {
  const { incidentId } = useParams()
  usePageTitle(incidentId)
  const [params, setParams] = useSearchParams()
  const { incident, error, loading } = useLiveIncident(incidentId)
  const limits = useRcaThresholds()
  const [openingReport, setOpeningReport] = useState(false)
  const active = incident ? isActiveIncident(incident) : false
  const now = useNow(active ? 1000 : 30000)
  const [refreshKey, setRefreshKey] = useState(0)

  const tab = TABS.includes(params.get('tab')) ? params.get('tab') : 'investigation'

  async function openReport() {
    setOpeningReport(true)
    try {
      await openIncidentDocument(incidentId)
    } catch (err) {
      notify.error(extractErrorMessage(err, 'Could not open the incident report.'))
    } finally {
      setOpeningReport(false)
    }
  }

  if (loading && !incident) {
    return (
      <div className="space-y-4" aria-busy="true">
        <SkeletonRows rows={2} className="p-0" />
        <SkeletonRows rows={5} className="p-0" />
      </div>
    )
  }

  if (!incident) {
    const notFound = error?.response?.status === 404
    return (
      <div className="rounded-lg border bg-card">
        {notFound ? (
          <EmptyState
            icon={SearchX}
            title="Incident not found"
            description={`There is no incident with ID ${incidentId}. It may have been mistyped, or belong to another environment.`}
            action={
              <Button asChild variant="outline">
                <Link to="/incidents">Back to incidents</Link>
              </Button>
            }
          />
        ) : (
          <ErrorState title="Couldn’t load this incident" message={extractErrorMessage(error, 'Sentinel may be unreachable.')} onRetry={() => window.location.reload()} />
        )}
      </div>
    )
  }

  const awaiting = isAwaitingHuman(incident)
  const hasReport = Boolean(incident.documentation?.markdown)
  const showFeedback = Boolean(incident.hypothesis) || (incident.attempts?.length ?? 0) > 0

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
            <h1 className="flex min-w-0 items-center gap-2 text-xl leading-7 font-semibold tracking-tight">
              <SeverityBadge severity={incident.severity} iconOnly />
              <span className="truncate">{incident.alertname}</span>
              <span className="font-normal text-muted-foreground">on {incident.app ?? 'unknown'}</span>
            </h1>
            <StatusBadge status={incidentStatus(incident)} />
            {(incident.occurrence ?? 1) > 1 && <Badge variant="outline">Occurrence #{incident.occurrence}</Badge>}
          </div>
          <p className="mt-1.5 max-w-3xl text-sm text-muted-foreground">{incidentHeadline(incident)}</p>
          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
            <span className="inline-flex items-center gap-0.5 font-mono">
              {incident.id}
              <CopyButton value={incident.id} label="Copy incident ID" className="size-6" />
            </span>
            <span>
              Started <Timestamp value={incident.created_at} />
            </span>
            <span className="tnum">{durationLabel(incident)} {formatSpan(incidentDuration(incident, now / 1000))}</span>
            {active && (
              <span className={cn('inline-flex items-center gap-1.5 font-medium', TONE_TEXT.info)}>
                <LiveDot /> Updating live
              </span>
            )}
          </div>
        </div>
        <Tooltip>
          <TooltipTrigger asChild>
            <span tabIndex={hasReport ? -1 : 0}>
              <Button variant="outline" size="sm" onClick={openReport} disabled={!hasReport || openingReport}>
                {openingReport ? <LoaderCircle className="animate-spin" /> : <FileText />} Open incident report
              </Button>
            </span>
          </TooltipTrigger>
          <TooltipContent>{hasReport ? 'The post-mortem Sentinel generated, in a new tab' : 'Sentinel writes the report at the end of the lifecycle'}</TooltipContent>
        </Tooltip>
      </header>

      <Panel title="Lifecycle" description="Where this incident is, and where time went">
        <LifecycleRail incident={incident} variant="full" />
        <OutcomeStrip incident={incident} now={now} />
      </Panel>

      <DecisionSummaryCard incident={incident} />

      {awaiting && <AttentionPanel incident={incident} onChanged={() => setRefreshKey((k) => k + 1)} key={`${incident.id}-${refreshKey}`} />}

      <Tabs value={tab} onValueChange={(next) => { const copy = new URLSearchParams(params); if (next === 'investigation') copy.delete('tab'); else copy.set('tab', next); setParams(copy, { replace: true }) }}>
        <TabsList variant="line" className="h-9 w-full justify-start border-b px-0">
          <TabsTrigger value="investigation" className="flex-none px-3">Investigation</TabsTrigger>
          <TabsTrigger value="evidence" className="flex-none px-3">Evidence</TabsTrigger>
          <TabsTrigger value="graph" className="flex-none px-3">Causal graph</TabsTrigger>
          <TabsTrigger value="replay" className="flex-none px-3">Replay</TabsTrigger>
          <TabsTrigger value="timeline" className="flex-none px-3">
            Timeline <span className="tnum rounded bg-muted px-1 text-[11px] text-muted-foreground">{incident.timeline?.length ?? 0}</span>
          </TabsTrigger>
          <TabsTrigger value="logs" className="flex-none px-3">Sentinel logs</TabsTrigger>
          {showFeedback && <TabsTrigger value="feedback" className="flex-none px-3">Feedback</TabsTrigger>}
        </TabsList>

        <TabsContent value="investigation" className="pt-4">
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_20rem]">
            <div className="min-w-0 space-y-4">
              <ObservedSection incident={incident} limits={limits} />
              <DiagnosisSection incident={incident} />
              <DecisionSection incident={incident} />
            </div>
            <aside className="min-w-0 xl:sticky xl:top-16 xl:self-start" aria-label="Incident facts">
              <FactsPanel incident={incident} now={now} />
            </aside>
          </div>
        </TabsContent>
        <TabsContent value="evidence" className="pt-4">
          <EvidenceTab incident={incident} />
        </TabsContent>
        <TabsContent value="graph" className="pt-4">
          {tab === 'graph' && <CausalGraphTab key={incident.id} incidentId={incident.id} />}
        </TabsContent>
        <TabsContent value="replay" className="pt-4">
          {tab === 'replay' && <ReplayTab key={incident.id} incidentId={incident.id} />}
        </TabsContent>
        <TabsContent value="timeline" className="pt-4">
          <TimelineTab incident={incident} />
        </TabsContent>
        <TabsContent value="logs" className="pt-4">
          {tab === 'logs' && <LogViewer incidentId={incident.id} height="h-[28rem]" />}
        </TabsContent>
        {showFeedback && (
          <TabsContent value="feedback" className="pt-4">
            <FeedbackTab incident={incident} />
          </TabsContent>
        )}
      </Tabs>
    </div>
  )
}
