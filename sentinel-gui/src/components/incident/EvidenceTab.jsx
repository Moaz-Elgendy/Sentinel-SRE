import { CopyButton } from '@/components/sentinel/CopyButton'
import { Panel } from '@/components/sentinel/Panel'
import { EmptyState } from '@/components/sentinel/States'
import { TONE_TEXT } from '@/components/sentinel/tone'
import { Badge } from '@/components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { cn } from '@/lib/utils'
import { formatBytes, formatClock, formatSpan, formatTimestamp, sentenceCase } from '@/utils/format'
import { Timestamp } from '@/components/sentinel/Timestamp'
import { SearchX } from 'lucide-react'

function Section({ title, description, children, count, flush = true }) {
  return (
    <Panel title={count != null ? `${title} (${count})` : title} description={description} flush={flush}>
      {children}
    </Panel>
  )
}

/** Everything Sentinel collected, verbatim, for when the summary isn't enough. */
export function EvidenceTab({ incident }) {
  const ev = incident.evidence
  if (!ev?.collected_at) {
    return <EmptyState icon={SearchX} title="No evidence recorded" description="Sentinel hasn’t collected evidence for this incident yet, or closed it before doing so." />
  }
  const pods = ev.pods ?? []
  const events = [...(ev.k8s_events ?? [])].sort((a, b) => b.at - a.at)
  const revisions = ev.replicaset_history ?? []
  const deployment = ev.deployment
  const checks = Object.entries(ev.health_checks ?? {})
  const chaos = Object.entries(ev.chaos_state ?? {})
  const samples = ev.log_sample_messages ?? []
  const deliveries = Object.entries(ev.notification_deliveries ?? {})
  const injections = Object.entries(ev.chaos_injections ?? {})

  return (
    <div className="space-y-4">
      <p className="text-xs text-muted-foreground">
        Collected <Timestamp value={ev.collected_at} /> ({formatTimestamp(ev.collected_at)}).
      </p>

      <div className="grid gap-4 xl:grid-cols-2">
        <Section title="Pods" count={pods.length} description="State when evidence was collected">
          {pods.length === 0 ? (
            <p className="px-4 py-3 text-sm text-muted-foreground">No pod data, likely because Kubernetes wasn’t reachable.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead>Pod</TableHead>
                  <TableHead>State</TableHead>
                  <TableHead className="text-right">Restarts</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {pods.map((pod) => (
                  <TableRow key={pod.name}>
                    <TableCell className="max-w-56 truncate font-mono text-xs" title={pod.name}>
                      {pod.name}
                    </TableCell>
                    <TableCell>
                      <div className="flex flex-wrap items-center gap-1">
                        <Badge variant={pod.ready ? 'ok' : 'bad'}>{pod.ready ? 'Ready' : 'Not ready'}</Badge>
                        {(pod.container_states ?? []).filter((c) => c.waiting_reason || c.terminated_reason || c.last_terminated_reason).map((c) => (
                          <Badge key={c.name} variant="outline" className="font-normal">
                            {c.waiting_reason ?? c.terminated_reason ?? `last: ${c.last_terminated_reason}`}
                          </Badge>
                        ))}
                      </div>
                    </TableCell>
                    <TableCell className={cn('tnum text-right', pod.restart_count > 0 && TONE_TEXT.warn)}>{pod.restart_count}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </Section>

        <Section title="Revisions" count={revisions.length} description="ReplicaSet history, newest first">
          {revisions.length === 0 ? (
            <p className="px-4 py-3 text-sm text-muted-foreground">No revision history available.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead>Rev</TableHead>
                  <TableHead>Image</TableHead>
                  <TableHead className="text-right">Ready</TableHead>
                  <TableHead className="text-right">Created</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {revisions.map((r, i) => (
                  <TableRow key={r.name}>
                    <TableCell className="tnum">
                      {r.revision}
                      {i === 0 && <Badge variant="secondary" className="ml-1.5">newest</Badge>}
                    </TableCell>
                    <TableCell className="max-w-56 truncate font-mono text-xs" title={(r.images ?? []).join(', ')}>
                      {(r.images ?? []).map((img) => img.split('/').pop()).join(', ')}
                    </TableCell>
                    <TableCell className={cn('tnum text-right', r.replicas > 0 && r.ready_replicas < r.replicas && TONE_TEXT.bad)}>
                      {r.ready_replicas}/{r.replicas}
                    </TableCell>
                    <TableCell className="text-right text-muted-foreground">
                      <Timestamp value={r.created_at} />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </Section>
      </div>

      <Section title="Kubernetes events" count={events.length} description="Newest first">
        {events.length === 0 ? (
          <p className="px-4 py-3 text-sm text-muted-foreground">No events recorded.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Time</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Reason</TableHead>
                <TableHead>Object</TableHead>
                <TableHead className="w-full">Message</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {events.map((e, i) => (
                <TableRow key={`${e.at}-${i}`}>
                  <TableCell className="tnum font-mono text-xs text-muted-foreground">{formatClock(e.at)}</TableCell>
                  <TableCell>
                    <Badge variant={e.type === 'Warning' ? 'warn' : 'neutral'}>{e.type}</Badge>
                  </TableCell>
                  <TableCell>
                    {e.reason}
                    {e.count > 1 && <span className="tnum ml-1 text-xs text-muted-foreground">×{e.count}</span>}
                  </TableCell>
                  <TableCell className="font-mono text-xs">{e.object}</TableCell>
                  <TableCell className="min-w-64 whitespace-normal">{e.message}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Section>

      <div className="grid gap-4 xl:grid-cols-2">
        {((deployment && Object.keys(deployment).length > 0) || checks.length > 0) && (
          <Section title="Deployment and health" flush={false}>
            <div className="space-y-3">
              {deployment && Object.keys(deployment).length > 0 && (
                <dl className="grid grid-cols-3 gap-3 text-sm">
                  {[
                    ['Desired', deployment.desired_replicas],
                    ['Ready', deployment.ready_replicas],
                    ['Available', deployment.available_replicas],
                  ].map(([label, value]) => (
                    <div key={label}>
                      <dt className="text-xs text-muted-foreground">{label} replicas</dt>
                      <dd className="tnum font-medium">{value ?? '—'}</dd>
                    </div>
                  ))}
                </dl>
              )}
              {checks.length > 0 && (
                <ul className="flex flex-wrap gap-1.5">
                  {checks.map(([name, status]) => (
                    <li key={name}>
                      <Badge variant={String(status).toLowerCase() === 'ok' ? 'ok' : 'warn'}>
                        {name}: {String(status)}
                      </Badge>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </Section>
        )}
        {chaos.length > 0 && (
          <Section title="Chaos state" description="Injected-fault gauges reported by the service" flush={false}>
            <dl className="space-y-2 text-sm">
              {chaos.map(([pod, gauges]) => (
                <div key={pod}>
                  <dt className="font-mono text-xs text-muted-foreground">{pod}</dt>
                  <dd className="font-mono text-xs">{JSON.stringify(gauges)}</dd>
                </div>
              ))}
            </dl>
          </Section>
        )}
        {(deliveries.length > 0 || ev.notification_dispatch_failures != null) && (
          <Section title="Notification service" description="Delivery counters reported by the service" flush={false}>
            <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-3">
              {deliveries.map(([channel, count]) => (
                <div key={channel}>
                  <dt className="text-xs text-muted-foreground">{sentenceCase(channel)}</dt>
                  <dd className="tnum font-medium">{count}</dd>
                </div>
              ))}
              {ev.notification_dispatch_failures != null && (
                <div>
                  <dt className="text-xs text-muted-foreground">Dispatch failures</dt>
                  <dd className="tnum font-medium">{ev.notification_dispatch_failures}</dd>
                </div>
              )}
            </dl>
          </Section>
        )}
        {injections.length > 0 && (
          <Section title="Chaos injections" description="Fault counters reported by the service" flush={false}>
            <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-3">
              {injections.map(([name, count]) => (
                <div key={name}>
                  <dt className="text-xs text-muted-foreground">{sentenceCase(name)}</dt>
                  <dd className="tnum font-medium">{count}</dd>
                </div>
              ))}
            </dl>
          </Section>
        )}
      </div>

      {samples.length > 0 && (
        <Section title="Log samples" count={samples.length} description="Error lines from Loki around the incident">
          <div className="relative">
            <pre className="max-h-72 overflow-auto bg-muted/30 px-4 py-3 font-mono text-xs leading-5 whitespace-pre-wrap">{samples.join('\n')}</pre>
            <div className="absolute top-1.5 right-1.5">
              <CopyButton value={samples.join('\n')} label="Copy log samples" />
            </div>
          </div>
        </Section>
      )}
      <p className="text-xs text-muted-foreground">
        Working set {formatBytes(ev.memory_bytes)}
        {ev.latest_revision_age_seconds != null && <> · newest revision {formatSpan(ev.latest_revision_age_seconds)} old</>}
      </p>
    </div>
  )
}
