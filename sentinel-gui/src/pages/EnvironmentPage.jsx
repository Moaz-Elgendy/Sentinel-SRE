import { CircleAlert, LoaderCircle, Plug, Server } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { extractErrorMessage } from '@/api/client'
import { listEnvironments, testEnvironmentConnection } from '@/api/environments'
import { ServicesPanel } from '@/components/dashboard/ServicesPanel'
import { KeyValue } from '@/components/sentinel/KeyValue'
import { Panel } from '@/components/sentinel/Panel'
import { PageHeader } from '@/components/sentinel/PageHeader'
import { Callout, EmptyState, ErrorState, SkeletonRows } from '@/components/sentinel/States'
import { SeverityBadge } from '@/components/sentinel/SeverityBadge'
import { StatusBadge, StatusIcon } from '@/components/sentinel/StatusBadge'
import { Timestamp } from '@/components/sentinel/Timestamp'
import { TONE_TEXT } from '@/components/sentinel/tone'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { useIncidentFeed } from '@/context/IncidentFeedContext'
import { useSummary } from '@/context/SummaryContext'
import { usePageTitle } from '@/hooks/usePageTitle'
import { usePolling } from '@/hooks/usePolling'
import { cn } from '@/lib/utils'
import { incidentStatus } from '@/utils/incident'
import { rootCauseLabel } from '@/utils/incident'

const CONNECTORS = [
  { key: 'kubernetes', name: 'Kubernetes', config: (e) => [e.kubernetes.mode, e.kubernetes.namespace && `namespace ${e.kubernetes.namespace}`] },
  { key: 'prometheus', name: 'Prometheus', config: (e) => [e.prometheus?.url] },
  { key: 'loki', name: 'Loki', config: (e) => [e.loki?.url] },
  { key: 'github', name: 'GitHub', config: (e) => [e.github?.repository] },
  { key: 'aws', name: 'AWS', config: (e) => [e.aws?.region] },
]

function Connector({ connector, environment, result }) {
  const ok = result?.ok
  const status = !result ? 'unknown' : ok === null ? 'unknown' : ok ? 'healthy' : 'down'
  const tone = !result || ok === null ? 'neutral' : ok ? 'ok' : 'bad'
  const word = !result ? 'Not tested yet' : ok === null ? 'Not configured' : ok ? 'Reachable' : 'Unreachable'
  const config = connector.config(environment).filter(Boolean)
  return (
    <li className="flex items-start gap-3 px-4 py-2.5">
      <StatusIcon status={status} className={cn('mt-0.5 size-4 shrink-0', TONE_TEXT[tone])} />
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium">{connector.name}</p>
        {config.length > 0 && <p className="truncate font-mono text-xs text-muted-foreground">{config.join(' · ')}</p>}
        {result?.detail && <p className="mt-0.5 text-xs text-muted-foreground">{result.detail}</p>}
      </div>
      <span className={cn('text-xs font-medium', TONE_TEXT[tone])}>{word}</span>
    </li>
  )
}

function EnvironmentSection({ environment, services, recentIncidents }) {
  const [result, setResult] = useState(null)
  const [testedAt, setTestedAt] = useState(null)
  const [testing, setTesting] = useState(false)
  const [error, setError] = useState(null)

  async function test() {
    setTesting(true)
    setError(null)
    try {
      setResult(await testEnvironmentConnection(environment.id))
      setTestedAt(Date.now() / 1000)
    } catch (err) {
      setError(extractErrorMessage(err, 'Could not run the connectivity test.'))
    } finally {
      setTesting(false)
    }
  }

  return (
    <section aria-label={environment.name} className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-card px-4 py-3">
        <div className="flex items-center gap-3">
          <span className="grid size-9 place-items-center rounded-md border bg-muted/40 text-muted-foreground">
            <Server aria-hidden="true" className="size-4.5" />
          </span>
          <div>
            <h2 className="text-base leading-5 font-semibold">{environment.name}</h2>
            <p className="text-xs text-muted-foreground">
              Customer {environment.customer_id} · Kubernetes {environment.kubernetes.mode}, namespace <span className="font-mono">{environment.kubernetes.namespace}</span>
            </p>
          </div>
        </div>
        <Button onClick={test} disabled={testing}>
          {testing ? <LoaderCircle className="animate-spin" /> : <Plug />} {testing ? 'Testing…' : 'Test connectivity'}
        </Button>
      </div>

      {error && <Callout tone="bad" icon={CircleAlert}>{error}</Callout>}

      <div className="grid gap-4 xl:grid-cols-2">
        <Panel
          title="Connectivity"
          description={testedAt ? undefined : 'Runs a live check of everything Sentinel depends on. Nothing is cached.'}
          actions={testedAt ? <span className="text-xs text-muted-foreground">Checked <Timestamp value={testedAt} /></span> : null}
          flush
        >
          <ul className="divide-y">
            {CONNECTORS.map((c) => (
              <Connector key={c.key} connector={c} environment={environment} result={result?.connectors?.[c.key]} />
            ))}
          </ul>
        </Panel>
        <ServicesPanel services={services} loading={false} />
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Panel title="Application profile" description={environment.application.name}>
          <div className="space-y-4">
            <KeyValue
              items={[
                { label: 'Application ID', value: environment.application.id, mono: true },
                { label: 'Deployments', value: environment.application.deployments.join(', '), mono: true },
              ]}
            />
            <div>
              <h3 className="mb-1.5 text-xs font-medium text-muted-foreground">Failure modes Sentinel watches for</h3>
              <ul className="flex flex-wrap gap-1.5">
                {environment.application.known_failure_modes.map((mode) => (
                  <li key={mode}>
                    <Badge variant="secondary" className="font-normal">
                      {rootCauseLabel(mode)}
                    </Badge>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </Panel>
        <Panel title="Recent incidents" description="Newest incidents in this environment" flush>
          {recentIncidents.length === 0 ? (
            <EmptyState compact title="No incidents recorded for this environment yet" />
          ) : (
            <ul className="divide-y">
              {recentIncidents.map((incident) => (
                <li key={incident.id}>
                  <Link to={`/incidents/${incident.id}`} className="flex items-center gap-3 px-4 py-2.5 outline-none hover:bg-muted/40 focus-visible:bg-muted/60">
                    <SeverityBadge severity={incident.severity} iconOnly />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium">
                        {incident.alertname} <span className="font-normal text-muted-foreground">on {incident.app}</span>
                      </span>
                      <span className="font-mono text-xs text-muted-foreground">{incident.id}</span>
                    </span>
                    <StatusBadge status={incidentStatus(incident)} />
                    <Timestamp value={incident.created_at} className="w-16 text-right text-xs text-muted-foreground" />
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>
    </section>
  )
}

export default function EnvironmentPage() {
  usePageTitle('Environment')
  const { data, error, loading, refetch } = usePolling(listEnvironments, { intervalMs: 30000 })
  const { data: summary } = useSummary()
  const { recent } = useIncidentFeed()
  const environments = data?.environments ?? []

  return (
    <div className="space-y-4">
      <PageHeader title="Environment" description="What Sentinel is watching, and whether it can reach everything it needs to see and act." />
      {loading && !data ? (
        <SkeletonRows rows={5} />
      ) : !data ? (
        <div className="rounded-lg border bg-card">
          <ErrorState title="Couldn’t load environments" message={extractErrorMessage(error, 'Sentinel did not respond.')} onRetry={refetch} />
        </div>
      ) : environments.length === 0 ? (
        <div className="rounded-lg border bg-card">
          <EmptyState icon={Server} title="No environment registered yet" description="Sentinel needs a registered environment before it can monitor or remediate anything." />
        </div>
      ) : (
        environments.map((environment) => (
          <EnvironmentSection
            key={environment.id}
            environment={environment}
            services={summary?.environment?.id === environment.id ? summary.system_health.services : []}
            recentIncidents={recent.filter((i) => i.environment_id === environment.id).slice(0, 6)}
          />
        ))
      )}
    </div>
  )
}
