import { Microscope } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { ConfidenceMeter } from '@/components/sentinel/Meters'
import { EmptyState } from '@/components/sentinel/States'
import { actionLabel, isActiveIncident, rootCauseLabel } from '@/utils/incident'
import { llmStatusLabel } from '@/utils/labels'
import { CaseStep } from './CaseStep.jsx'

/** Step 2: what Sentinel believes is happening, how sure it is, and why. */
export function DiagnosisSection({ incident }) {
  const h = incident.hypothesis
  if (!h) {
    return (
      <CaseStep step="2" title="What it concluded" description="Sentinel’s diagnosis and reasoning">
        <EmptyState compact icon={Microscope} title={isActiveIncident(incident) ? 'Diagnosis in progress' : 'No diagnosis was produced'} description={isActiveIncident(incident) ? 'Sentinel is still correlating evidence.' : 'The incident ended before Sentinel reached a diagnosis.'} />
      </CaseStep>
    )
  }
  const unknown = h.root_cause === 'unknown'
  const usedLlm = h.llm_used
  const rulesConfidence = h.rule_confidence
  const adjusted = usedLlm && rulesConfidence != null && Math.abs(rulesConfidence - h.confidence) >= 0.005

  return (
    <CaseStep step="2" title="What it concluded" description="Sentinel’s diagnosis and reasoning">
      <div className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_16rem] sm:items-end">
          <div>
            <p className="text-xs text-muted-foreground">Root cause</p>
            <p className="mt-0.5 text-lg leading-6 font-semibold">{unknown ? 'Not recognised' : rootCauseLabel(h.root_cause)}</p>
            <p className="mt-1.5 flex flex-wrap items-center gap-1.5">
              <Badge variant="secondary">{usedLlm ? 'Rules + LLM' : 'Rules'}</Badge>
              <Badge variant="outline" className="font-normal text-muted-foreground">
                {llmStatusLabel(h.llm_status)}
              </Badge>
            </p>
          </div>
          <div>
            <p className="mb-1 text-xs text-muted-foreground">Confidence</p>
            <ConfidenceMeter value={h.confidence} />
            {adjusted && (
              <p className="tnum mt-1 text-[11px] text-muted-foreground">
                Rules alone: {Math.round(rulesConfidence * 100)}%. The LLM moved it to {Math.round(h.confidence * 100)}%.
              </p>
            )}
          </div>
        </div>

        {h.reasoning && (
          <div className="border-t pt-3.5">
            <h3 className="text-xs font-medium text-muted-foreground">Why Sentinel thinks so</h3>
            <p className="mt-1 max-w-3xl text-sm leading-6">{h.reasoning}</p>
            {h.llm_note && <p className="mt-1.5 max-w-3xl text-xs text-muted-foreground">{h.llm_note}</p>}
          </div>
        )}

        {h.supporting?.length > 0 && (
          <div>
            <h3 className="text-xs font-medium text-muted-foreground">Supporting signals</h3>
            <ul className="mt-1.5 flex flex-wrap gap-1.5">
              {h.supporting.map((s) => (
                <li key={s}>
                  <Badge variant="outline" className="font-normal">
                    {s}
                  </Badge>
                </li>
              ))}
            </ul>
          </div>
        )}

        <p className="border-t pt-3 text-xs text-muted-foreground">
          Recommended action: <span className="font-medium text-foreground">{actionLabel(h.recommended_action)}</span>
        </p>
      </div>
    </CaseStep>
  )
}
