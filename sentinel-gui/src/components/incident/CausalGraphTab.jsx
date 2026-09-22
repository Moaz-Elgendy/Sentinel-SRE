import { useEffect, useState } from 'react'
import { GitBranch } from 'lucide-react'
import { getCausalGraph } from '@/api/incidents'
import { extractErrorMessage } from '@/api/client'
import { Panel } from '@/components/sentinel/Panel'
import { EmptyState, ErrorState, SkeletonRows } from '@/components/sentinel/States'
import { Timestamp } from '@/components/sentinel/Timestamp'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'

// The only place edge/node "kind" is mapped to a color — kept in sync with
// causal_graph.py's own docstring, which defines what each kind means.
// Never blur these: a correlation must never render as if it were a fact.
const KIND_STYLE = {
  fact: 'border-neutral-solid/40 bg-muted/30 text-foreground',
  correlation: 'border-warn-edge bg-warn-tint text-warn',
  hypothesis: 'border-info-edge bg-info-tint text-info',
  action: 'border-border bg-muted/40 text-foreground',
  outcome: 'border-ok-edge bg-ok-tint text-ok',
}

const KIND_LABEL = {
  fact: 'Fact',
  correlation: 'Correlation',
  hypothesis: 'Hypothesis',
  action: 'Action',
  outcome: 'Outcome',
}

function KindBadge({ kind }) {
  return (
    <Badge variant="outline" className={cn('shrink-0 border px-1.5 py-0 text-[10px] font-medium capitalize', KIND_STYLE[kind] ?? KIND_STYLE.fact)}>
      {KIND_LABEL[kind] ?? kind}
    </Badge>
  )
}

const RISK_TONE = { low: 'text-ok', moderate: 'text-warn', high: 'text-bad' }

/** The one nested object `detail` can hold (the risk assessment on an
 * action node — see risk.py). Rendered as its own compact line instead of
 * falling into the generic key/value grid below, which only knows how to
 * print primitives and arrays. */
function RiskLine({ risk }) {
  if (!risk) return null
  return (
    <p className="mt-1.5 text-xs">
      <span className="text-muted-foreground/70">risk: </span>
      <span className={cn('font-medium capitalize', RISK_TONE[risk.level] ?? '')}>{risk.level}</span>
      <span className="text-muted-foreground"> — {risk.blast_radius_scope.replace(/_/g, ' ')}, {risk.reversible ? 'reversible' : 'not reversible'}, validation {risk.validation_available ? 'available' : 'unavailable'}</span>
    </p>
  )
}

function NodeCard({ node }) {
  const detailEntries = Object.entries(node.detail ?? {}).filter(
    ([k, v]) =>
      k !== 'risk' &&
      v !== null &&
      v !== undefined &&
      v !== '' &&
      !(typeof v === 'object' && !Array.isArray(v)) &&
      !(Array.isArray(v) && v.length === 0)
  )
  return (
    <div className="rounded-md border px-3 py-2">
      <div className="flex items-center gap-2">
        <KindBadge kind={node.kind} />
        <p className="min-w-0 truncate text-sm font-medium">{node.label}</p>
        {node.at != null && <Timestamp value={node.at} className="ml-auto shrink-0 text-xs text-muted-foreground" />}
      </div>
      {detailEntries.length > 0 && (
        <dl className="mt-1.5 grid grid-cols-2 gap-x-4 gap-y-0.5 text-xs text-muted-foreground sm:grid-cols-3">
          {detailEntries.slice(0, 9).map(([k, v]) => (
            <div key={k} className="min-w-0 truncate">
              <span className="text-muted-foreground/70">{k.replace(/_/g, ' ')}: </span>
              <span className="text-foreground">{Array.isArray(v) ? v.join(', ') : String(v)}</span>
            </div>
          ))}
        </dl>
      )}
      <RiskLine risk={node.detail?.risk} />
    </div>
  )
}

/**
 * Evidence-backed causal graph, integrated as a tab on the existing incident
 * page rather than a separate view. Nodes are grouped by kind (fact /
 * correlation / hypothesis / action / outcome); edges are listed as
 * "source -> target" so the link between a fact and Sentinel's belief (or
 * between a belief and the action it led to) stays legible without a
 * force-directed layout this console doesn't otherwise use.
 */
export function CausalGraphTab({ incidentId }) {
  const [state, setState] = useState({ loading: true, error: null, graph: null })

  // Remounted with a `key={incidentId}` by the caller when the incident
  // changes, so this effect never needs to reset state mid-lifetime itself
  // (that would mean calling setState synchronously inside the effect body).
  useEffect(() => {
    let cancelled = false
    getCausalGraph(incidentId)
      .then((graph) => {
        if (!cancelled) setState({ loading: false, error: null, graph })
      })
      .catch((err) => {
        if (!cancelled) setState({ loading: false, error: extractErrorMessage(err, 'Could not load the causal graph.'), graph: null })
      })
    return () => {
      cancelled = true
    }
  }, [incidentId])

  if (state.loading) return <SkeletonRows rows={5} className="p-0" />
  if (state.error) return <ErrorState message={state.error} />

  const { nodes = [], edges = [] } = state.graph ?? {}
  if (nodes.length === 0) {
    return (
      <EmptyState
        icon={GitBranch}
        title="Nothing to graph yet"
        description="Sentinel hasn't recorded any evidence, hypothesis, or action for this incident yet."
      />
    )
  }

  const labelById = Object.fromEntries(nodes.map((n) => [n.id, n.label]))
  const grouped = nodes.reduce((acc, n) => {
    ;(acc[n.kind] ??= []).push(n)
    return acc
  }, {})
  const groupOrder = ['fact', 'correlation', 'hypothesis', 'action', 'outcome'].filter((k) => grouped[k]?.length)

  return (
    <div className="space-y-4">
      <p className="text-xs text-muted-foreground">
        What Sentinel observed, believed, and did for this incident — derived from the incident record itself, so it can never
        show anything the rest of this page doesn't also show.
      </p>

      {groupOrder.map((kind) => (
        <Panel key={kind} title={KIND_LABEL[kind]} flush>
          <div className="space-y-2 px-4 py-3">
            {grouped[kind].map((n) => (
              <NodeCard key={n.id} node={n} />
            ))}
          </div>
        </Panel>
      ))}

      {edges.length > 0 && (
        <Panel title="Links" description="What connects what, and whether that link is an observed fact, a correlation, a belief, an action, or an outcome." flush>
          <div className="divide-y">
            {edges.map((e, i) => (
              <div key={i} className="flex flex-wrap items-center gap-x-2 gap-y-1 px-4 py-2 text-xs">
                <KindBadge kind={e.kind} />
                <span className="min-w-0 truncate font-medium">{labelById[e.source] ?? e.source}</span>
                <span className="text-muted-foreground">{e.label}</span>
                <span className="min-w-0 truncate font-medium">{labelById[e.target] ?? e.target}</span>
                {e.evidence?.length > 0 && <span className="w-full text-muted-foreground/70">{e.evidence.join('; ')}</span>}
              </div>
            ))}
          </div>
        </Panel>
      )}
    </div>
  )
}
