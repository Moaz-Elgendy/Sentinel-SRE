import { useCallback, useEffect, useState } from 'react'
import { History, PlayCircle, RefreshCw } from 'lucide-react'
import { getIncidentReplay } from '@/api/incidents'
import { extractErrorMessage } from '@/api/client'
import { Panel } from '@/components/sentinel/Panel'
import { EmptyState, ErrorState, SkeletonRows } from '@/components/sentinel/States'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { actionLabel, rootCauseLabel } from '@/utils/incident'
import { cn } from '@/lib/utils'

// Same convention as CausalGraphTab's RISK_TONE — the one place risk level
// is mapped to a color, kept in sync with risk.py's three-value table.
const RISK_TONE = { low: 'text-ok', moderate: 'text-warn', high: 'text-bad' }

function CandidateCard({ candidate }) {
  return (
    <div
      className={cn(
        'rounded-md border px-3 py-2.5',
        candidate.would_execute && 'border-l-4 border-l-ok-solid'
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-sm font-medium">{actionLabel(candidate.action)}</p>
        <Badge variant={candidate.allowed ? 'secondary' : 'outline'} className="font-normal">
          {candidate.allowed ? 'Allowed' : 'Denied'}
        </Badge>
        {candidate.would_execute && (
          <Badge variant="outline" className="border-ok-edge bg-ok-tint font-normal text-ok">
            Would execute
          </Badge>
        )}
        <span className="tnum ml-auto text-xs text-muted-foreground">
          confidence {Math.round(candidate.confidence * 100)}%
        </span>
      </div>

      <p className="mt-1.5 text-xs">
        <span className="text-muted-foreground/70">risk: </span>
        <span className={cn('font-medium capitalize', RISK_TONE[candidate.risk?.level] ?? '')}>
          {candidate.risk?.level ?? 'unknown'}
        </span>
        <span className="text-muted-foreground">
          {' '}
          — {candidate.risk?.blast_radius_scope?.replace(/_/g, ' ')},{' '}
          {candidate.risk?.reversible ? 'reversible' : 'not reversible'}, validation{' '}
          {candidate.risk?.validation_available ? 'available' : 'unavailable'}
        </span>
      </p>

      {!candidate.allowed && candidate.denial_detail && (
        <p className="mt-1 text-xs text-muted-foreground">{candidate.denial_detail}</p>
      )}
      <p className="mt-1 text-xs text-muted-foreground/70">{candidate.rationale}</p>
    </div>
  )
}

/**
 * Incident replay / what-if simulation, integrated as a tab on the existing
 * incident page (sentinel-ai app/lifecycle/replay.py). Answers "what would
 * Sentinel's current rules decide from this incident's recorded evidence" —
 * read-only, on demand, never a live re-investigation and never an
 * execution.
 */
export function ReplayTab({ incidentId }) {
  const [fromScratch, setFromScratch] = useState(false)
  const [state, setState] = useState({ loading: true, error: null, result: null })

  const load = useCallback(
    (scratch) => {
      setState((s) => ({ ...s, loading: true, error: null }))
      getIncidentReplay(incidentId, { fromScratch: scratch })
        .then((result) => setState({ loading: false, error: null, result }))
        .catch((err) =>
          setState({ loading: false, error: extractErrorMessage(err, 'Could not run the replay.'), result: null })
        )
    },
    [incidentId]
  )

  useEffect(() => {
    load(fromScratch)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [incidentId])

  if (state.loading) return <SkeletonRows rows={5} className="p-0" />
  if (state.error) return <ErrorState message={state.error} />

  const r = state.result
  const hasEvidence = !!r?.hypothesis

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <p className="flex-1 text-xs text-muted-foreground">
          What Sentinel's current rules, decision ladder, risk assessment and policy would conclude from this
          incident's recorded evidence — never a live re-investigation, never an execution.
        </p>
        <label className="flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">
          <input
            type="checkbox"
            checked={fromScratch}
            onChange={(e) => {
              setFromScratch(e.target.checked)
              load(e.target.checked)
            }}
          />
          Reconsider already-tried actions
        </label>
        <Button variant="outline" size="sm" onClick={() => load(fromScratch)}>
          <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
          Re-run
        </Button>
      </div>

      {!hasEvidence && (
        <EmptyState
          icon={History}
          title="Nothing to replay"
          description={r?.notes?.[0] ?? 'No evidence was recorded for this incident.'}
        />
      )}

      {hasEvidence && (
        <>
          <Panel title="Recomputed diagnosis" flush>
            <div className="space-y-1.5 px-4 py-3">
              <p className="text-sm">
                <span className="font-medium">{rootCauseLabel(r.hypothesis.root_cause)}</span>
                <span className="tnum text-muted-foreground"> — {Math.round(r.hypothesis.confidence * 100)}% confidence</span>
              </p>
              {r.hypothesis_root_cause_changed && (
                <p className="rounded border border-warn-edge bg-warn-tint px-2 py-1.5 text-xs text-warn">
                  Differs from the recorded diagnosis ({rootCauseLabel(r.recorded_root_cause)}). Replay always uses
                  the deterministic rules pass, never the LLM, so this can mean the original was LLM-adjusted or
                  that the RCA rules changed since this incident.
                </p>
              )}
            </div>
          </Panel>

          {r.candidates.length > 0 ? (
            <Panel
              title="Candidate actions"
              description={
                r.would_escalate
                  ? 'Every candidate is denied — Sentinel would escalate.'
                  : 'In the order Sentinel would try them.'
              }
              flush
            >
              <div className="space-y-2 px-4 py-3">
                {r.candidates.map((c) => (
                  <CandidateCard key={c.candidate_index} candidate={c} />
                ))}
              </div>
            </Panel>
          ) : (
            <EmptyState
              icon={PlayCircle}
              compact
              title="No candidate action"
              description="Sentinel would escalate — no safe autonomous action is available for this root cause."
            />
          )}

          {r.notes.length > 0 && (
            <ul className="space-y-1 text-xs text-muted-foreground/70">
              {r.notes.map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  )
}
