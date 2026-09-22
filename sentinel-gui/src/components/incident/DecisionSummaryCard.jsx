import { ArrowRight, ShieldX } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Panel } from '@/components/sentinel/Panel'
import { ConfidenceMeter } from '@/components/sentinel/Meters'
import { TONE_TEXT } from '@/components/sentinel/tone'
import { cn } from '@/lib/utils'
import { actionSummary, isActiveIncident, isDiagnosed, rootCauseLabel } from '@/utils/incident'
import { denialReasonLabel } from '@/utils/labels'

/**
 * "Sentinel decision": a one-glance synthesis of what the case steps below
 * say in full (see ObservedSection / DiagnosisSection / DecisionSection) —
 * the diagnosis and the action Sentinel chose because of it — so a reader
 * gets the gist without opening every step first. It never states a fact
 * the case steps don't already show in full: same `incident.hypothesis`
 * and `actionSummary()` the Diagnosis/Decision steps themselves read.
 *
 * Deliberately leaves the OUTCOME (recovered / awaiting / in progress) to
 * the Lifecycle panel's OutcomeStrip just above it on the page — this card
 * is "what it decided", that one is "how it turned out"; showing both in
 * the same box would just repaint the same state twice.
 */
export function DecisionSummaryCard({ incident }) {
  const h = incident.hypothesis
  const action = actionSummary(incident)

  // Nothing to synthesize yet — the case steps' own empty states already
  // say "diagnosis in progress" / "no action decided yet", so staying
  // silent here rather than showing an empty card is the honest choice.
  if (!h && !action) return null

  const diagnosed = isDiagnosed(incident)

  return (
    <Panel title="Sentinel decision" description="What it concluded, and what it did because of it">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <div className="min-w-0">
          <p className="text-xs text-muted-foreground">Diagnosis</p>
          {h ? (
            <div className="mt-0.5 flex items-center gap-2">
              <p className="truncate text-sm font-medium">{diagnosed ? rootCauseLabel(h.root_cause) : 'Not recognised'}</p>
              <ConfidenceMeter value={h.confidence} size="sm" className="w-20 min-w-0" />
            </div>
          ) : (
            <p className="mt-0.5 text-sm text-muted-foreground">{isActiveIncident(incident) ? 'Still investigating' : 'No diagnosis was produced'}</p>
          )}
        </div>

        <ArrowRight aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />

        <div className="min-w-0">
          <p className="text-xs text-muted-foreground">{action?.kind === 'blocked' ? 'Blocked action' : 'Action taken'}</p>
          {action ? (
            <div className="mt-0.5 flex flex-wrap items-center gap-1.5">
              {action.kind === 'blocked' && <ShieldX aria-hidden="true" className={cn('size-3.5', TONE_TEXT.warn)} />}
              <p className="truncate text-sm font-medium">{action.label}</p>
              {action.kind === 'blocked' && <span className="text-xs text-muted-foreground">— {denialReasonLabel(action.reason)}</span>}
              {action.kind === 'executed' && !action.ok && <Badge variant="bad">Did not recover</Badge>}
              {action.kind === 'executed' && action.dryRun && <Badge variant="warn">Dry run</Badge>}
              {action.human && <Badge variant="info">SRE authorized</Badge>}
            </div>
          ) : (
            <p className="mt-0.5 text-sm text-muted-foreground">{isActiveIncident(incident) ? 'No action decided yet' : 'None attempted'}</p>
          )}
        </div>
      </div>
    </Panel>
  )
}
