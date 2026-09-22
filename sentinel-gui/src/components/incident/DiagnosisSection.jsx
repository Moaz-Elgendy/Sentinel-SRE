import { History, Microscope } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { ConfidenceMeter } from '@/components/sentinel/Meters'
import { EmptyState } from '@/components/sentinel/States'
import { TONE_TEXT } from '@/components/sentinel/tone'
import { actionLabel, isActiveIncident, rootCauseLabel } from '@/utils/incident'
import { llmStatusLabel } from '@/utils/labels'
import { CaseStep } from './CaseStep.jsx'
import { cn } from '@/lib/utils'

// memory.py's `as_supporting_note()` always starts with this exact prefix —
// kept in sync deliberately. "Seen before" citations are full sentences
// about a *different* incident, not a short tag about this one, so they get
// their own list instead of being squeezed into a "Supporting signals" pill,
// and a tone drawn from the fixed phrases that function produces (escalated
// / failed / unconfirmed / succeeded), so a skim tells you at a glance
// whether Sentinel's past attempts on similar incidents actually worked.
const MEMORY_NOTE_PREFIX = "similar past incident "

function memoryNoteTone(note) {
  if (note.includes('did not resolve the incident')) return 'bad'
  if (note.includes('was escalated to a human')) return 'warn'
  if (note.includes('recovery was not confirmed')) return 'warn'
  if (note.includes('resolved autonomously via')) return 'ok'
  return 'neutral'
}

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
  const allSupporting = h.supporting ?? []
  const memoryNotes = allSupporting.filter((s) => s.startsWith(MEMORY_NOTE_PREFIX))
  const ruleSupporting = allSupporting.filter((s) => !s.startsWith(MEMORY_NOTE_PREFIX))

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

        {ruleSupporting.length > 0 && (
          <div>
            <h3 className="text-xs font-medium text-muted-foreground">Supporting signals</h3>
            <ul className="mt-1.5 flex flex-wrap gap-1.5">
              {ruleSupporting.map((s) => (
                <li key={s}>
                  <Badge variant="outline" className="font-normal">
                    {s}
                  </Badge>
                </li>
              ))}
            </ul>
          </div>
        )}

        {memoryNotes.length > 0 && (
          <div>
            <h3 className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
              <History className="h-3.5 w-3.5" aria-hidden="true" />
              Seen before
            </h3>
            <ul className="mt-1.5 space-y-1">
              {memoryNotes.map((s) => (
                <li key={s} className={cn('text-sm leading-6', TONE_TEXT[memoryNoteTone(s)])}>
                  {s}
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
