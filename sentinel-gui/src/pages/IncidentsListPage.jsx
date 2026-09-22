import { ArrowDownUp, CircleCheck, Hand, ListFilter, LoaderCircle, RefreshCw, Siren, TriangleAlert } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { FilterSelect, SearchInput } from '@/components/sentinel/FilterBar'
import { IncidentCard } from '@/components/sentinel/IncidentCard'
import { LifecycleRail } from '@/components/sentinel/LifecycleRail'
import { Pager } from '@/components/sentinel/Pager'
import { PageHeader } from '@/components/sentinel/PageHeader'
import { EmptyState, ErrorState, SkeletonRows } from '@/components/sentinel/States'
import { SeverityBadge } from '@/components/sentinel/SeverityBadge'
import { StatusBadge } from '@/components/sentinel/StatusBadge'
import { Timestamp } from '@/components/sentinel/Timestamp'
import { TONE_TEXT } from '@/components/sentinel/tone'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useNow } from '@/hooks/useNow'
import { usePage } from '@/hooks/usePage'
import { useIncidentList } from '@/hooks/useIncidentList'
import { usePageTitle } from '@/hooks/usePageTitle'
import { cn } from '@/lib/utils'
import { formatSpan } from '@/utils/format'
import {
  actionSummary,
  incidentDuration,
  incidentHeadline,
  incidentStatus,
  isActiveIncident,
  isAwaitingHuman,
  isDiagnosed,
  isResolvedIncident,
  rootCauseLabel,
} from '@/utils/incident'

const PAGE_SIZE = 25

const VIEWS = [
  { value: 'all', label: 'All', test: () => true },
  { value: 'active', label: 'In progress', test: isActiveIncident },
  { value: 'attention', label: 'Needs you', test: isAwaitingHuman },
  { value: 'resolved', label: 'Resolved', test: isResolvedIncident },
]

const SEVERITY_ORDER = { critical: 0, warning: 1, info: 2 }
const SORTS = {
  newest: { label: 'Newest first', cmp: (a, b) => b.created_at - a.created_at },
  oldest: { label: 'Oldest first', cmp: (a, b) => a.created_at - b.created_at },
  severity: { label: 'Severity', cmp: (a, b) => (SEVERITY_ORDER[a.severity] ?? 9) - (SEVERITY_ORDER[b.severity] ?? 9) || b.created_at - a.created_at },
  longest: { label: 'Longest duration', cmp: (a, b) => incidentDuration(b) - incidentDuration(a) },
}

function ActionCell({ incident }) {
  const summary = actionSummary(incident)
  if (!summary) {
    return <span className="text-muted-foreground">{isAwaitingHuman(incident) ? 'No action taken' : '—'}</span>
  }
  if (summary.kind === 'blocked') {
    return (
      <span className={cn('inline-flex items-center gap-1.5', TONE_TEXT.warn)} title={`${summary.label} was blocked by policy`}>
        <TriangleAlert aria-hidden="true" className="size-3.5" /> Blocked: {summary.label.toLowerCase()}
      </span>
    )
  }
  return (
    <span className="inline-flex items-center gap-1.5">
      {summary.ok ? <CircleCheck aria-label="Succeeded" className={cn('size-3.5', TONE_TEXT.ok)} /> : <TriangleAlert aria-label="Did not succeed" className={cn('size-3.5', TONE_TEXT.bad)} />}
      {summary.label}
      {summary.human && <span className="rounded border px-1 text-[11px] text-muted-foreground">SRE</span>}
      {summary.dryRun && <span className="rounded border px-1 text-[11px] text-muted-foreground">Dry run</span>}
    </span>
  )
}

function Duration({ incident }) {
  const active = isActiveIncident(incident)
  const now = useNow(active ? 1000 : 60000)
  return <span className="tnum">{formatSpan(incidentDuration(incident, now / 1000))}</span>
}

export default function IncidentsListPage() {
  usePageTitle('Incidents')
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const { incidents, loading, error, refetch, canLoadOlder, loadOlder, loadingOlder, olderError } = useIncidentList()

  const view = VIEWS.some((v) => v.value === params.get('view')) ? params.get('view') : 'all'
  const [query, setQuery] = useState('')
  const [severity, setSeverity] = useState('all')
  const [service, setService] = useState('all')
  const [sort, setSort] = useState('newest')
  const [page, setPage] = usePage(`${view}|${query}|${severity}|${service}|${sort}`)

  const services = useMemo(() => [...new Set(incidents.map((i) => i.app).filter(Boolean))].sort(), [incidents])

  const counts = useMemo(() => Object.fromEntries(VIEWS.map((v) => [v.value, incidents.filter(v.test).length])), [incidents])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    const test = VIEWS.find((v) => v.value === view).test
    return incidents
      .filter(test)
      .filter((i) => severity === 'all' || i.severity === severity)
      .filter((i) => service === 'all' || i.app === service)
      .filter((i) => {
        if (!q) return true
        const haystack = [i.id, i.alertname, i.app, i.namespace, i.summary, i.hypothesis?.root_cause && rootCauseLabel(i.hypothesis.root_cause)]
          .filter(Boolean)
          .join(' ')
          .toLowerCase()
        return haystack.includes(q)
      })
      .sort(SORTS[sort].cmp)
  }, [incidents, view, query, severity, service, sort])

  const pageRows = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)
  const hasFilters = query || severity !== 'all' || service !== 'all'

  function setView(next) {
    const copy = new URLSearchParams(params)
    if (next === 'all') copy.delete('view')
    else copy.set('view', next)
    setParams(copy, { replace: true })
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Incidents"
        description="Every incident Sentinel has detected: what it found, what it did, and how it ended."
        actions={
          <Button variant="outline" size="sm" onClick={refetch}>
            <RefreshCw /> Refresh
          </Button>
        }
      />

      <Tabs value={view} onValueChange={setView}>
        <TabsList variant="line" className="h-9 w-full justify-start border-b px-0">
          {VIEWS.map((v) => (
            <TabsTrigger key={v.value} value={v.value} className="flex-none px-3">
              {v.value === 'attention' && <Hand />}
              {v.label}
              <span className={cn('tnum rounded px-1 text-[11px]', v.value === 'attention' && counts.attention > 0 ? 'bg-warn-tint text-warn' : 'bg-muted text-muted-foreground')}>{counts[v.value]}</span>
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      <div className="flex flex-wrap items-center gap-2">
        <SearchInput value={query} onChange={setQuery} placeholder="Search by ID, alert, service or diagnosis" className="w-full sm:w-80" label="Search incidents" />
        <FilterSelect
          label="Severity"
          value={severity}
          onChange={setSeverity}
          width="w-40"
          options={[
            { value: 'all', label: 'All' },
            { value: 'critical', label: 'Critical' },
            { value: 'warning', label: 'Warning' },
            { value: 'info', label: 'Info' },
          ]}
        />
        <FilterSelect label="Service" value={service} onChange={setService} width="w-52" options={[{ value: 'all', label: 'All' }, ...services.map((s) => ({ value: s, label: s }))]} />
        <FilterSelect label="Sort" value={sort} onChange={setSort} width="w-52" options={Object.entries(SORTS).map(([value, s]) => ({ value, label: s.label }))} />
        {hasFilters && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setQuery('')
              setSeverity('all')
              setService('all')
            }}
          >
            Clear filters
          </Button>
        )}
        <div className="ml-auto">
          <Pager page={page} pageSize={PAGE_SIZE} total={filtered.length} onPageChange={setPage} noun="incidents" />
        </div>
      </div>

      <Card className="gap-0 overflow-hidden">
        {loading ? (
          <SkeletonRows rows={8} />
        ) : error ? (
          <ErrorState title="Could not load incidents" onRetry={refetch} />
        ) : filtered.length === 0 ? (
          <EmptyState
            icon={hasFilters ? ListFilter : Siren}
            title={hasFilters ? 'No incidents match these filters' : view === 'attention' ? 'Nothing is waiting on you' : 'No incidents yet'}
            description={
              hasFilters
                ? 'Try a broader search or clear the filters.'
                : view === 'attention'
                  ? 'Every incident Sentinel escalated has been decided.'
                  : 'When Alertmanager fires an alert, Sentinel opens an incident here and works it end to end.'
            }
          />
        ) : (
          <>
            {/* Phones get the same incident cards as the command center: a table would clip status and time. */}
            <ul className="divide-y md:hidden">
              {pageRows.map((incident) => (
                <li key={incident.id}>
                  <IncidentCard incident={incident} />
                </li>
              ))}
            </ul>
          <Table className="hidden md:table">
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Incident</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="hidden xl:table-cell">Lifecycle</TableHead>
                <TableHead className="hidden lg:table-cell">Diagnosis and action</TableHead>
                <TableHead className="hidden md:table-cell">
                  <span className="inline-flex items-center gap-1">
                    <ArrowDownUp aria-hidden="true" className="size-3" /> Duration
                  </span>
                </TableHead>
                <TableHead className="text-right">Started</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {pageRows.map((incident) => (
                <TableRow
                  key={incident.id}
                  tabIndex={0}
                  onClick={() => navigate(`/incidents/${incident.id}`)}
                  onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), navigate(`/incidents/${incident.id}`))}
                  className="cursor-pointer focus-visible:bg-muted/60 focus-visible:outline-none"
                >
                  <TableCell className="max-w-96 whitespace-normal">
                    <div className="flex items-center gap-2">
                      <SeverityBadge severity={incident.severity} iconOnly />
                      <span className="truncate font-medium">{incident.alertname}</span>
                      <span className="truncate text-muted-foreground">on {incident.app ?? '—'}</span>
                    </div>
                    <p className="mt-0.5 truncate text-xs text-muted-foreground">
                      <span className="font-mono">{incident.id}</span> <span className="ml-1">{incidentHeadline(incident)}</span>
                    </p>
                  </TableCell>
                  <TableCell>
                    <StatusBadge status={incidentStatus(incident)} />
                  </TableCell>
                  <TableCell className="hidden w-44 xl:table-cell">
                    <LifecycleRail incident={incident} variant="mini" />
                  </TableCell>
                  <TableCell className="hidden lg:table-cell">
                    {isDiagnosed(incident) ? (
                      <p>
                        {rootCauseLabel(incident.hypothesis.root_cause)} <span className="tnum text-xs text-muted-foreground">{Math.round(incident.hypothesis.confidence * 100)}%</span>
                      </p>
                    ) : (
                      <p className="text-muted-foreground">{isActiveIncident(incident) ? 'Diagnosing…' : 'Not diagnosed'}</p>
                    )}
                    <div className="mt-0.5 text-xs">
                      <ActionCell incident={incident} />
                    </div>
                  </TableCell>
                  <TableCell className="hidden md:table-cell">
                    <Duration incident={incident} />
                  </TableCell>
                  <TableCell className="text-right text-muted-foreground">
                    <Timestamp value={incident.created_at} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          </>
        )}
      </Card>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-xs text-muted-foreground">
          {canLoadOlder ? `Showing the newest ${incidents.length} incidents.` : `${incidents.length} incident${incidents.length === 1 ? '' : 's'} loaded.`}
          {olderError && <span className={cn('ml-2', TONE_TEXT.bad)}>{olderError}</span>}
        </p>
        {canLoadOlder && (
          <Button variant="outline" size="sm" onClick={loadOlder} disabled={loadingOlder}>
            {loadingOlder && <LoaderCircle className="animate-spin" />} Load older incidents
          </Button>
        )}
      </div>
    </div>
  )
}
