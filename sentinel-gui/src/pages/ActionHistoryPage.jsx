import { ListChecks, SearchX } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { extractErrorMessage } from '@/api/client'
import { listActions } from '@/api/actions'
import { listActionTypes } from '@/api/meta'
import { FilterSelect, SearchInput } from '@/components/sentinel/FilterBar'
import { KeyValue } from '@/components/sentinel/KeyValue'
import { ConfidenceMeter } from '@/components/sentinel/Meters'
import { Pager } from '@/components/sentinel/Pager'
import { PageHeader } from '@/components/sentinel/PageHeader'
import { Callout, EmptyState, ErrorState, SkeletonRows } from '@/components/sentinel/States'
import { StatusBadge } from '@/components/sentinel/StatusBadge'
import { Timestamp } from '@/components/sentinel/Timestamp'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { usePage } from '@/hooks/usePage'
import { usePageTitle } from '@/hooks/usePageTitle'
import { usePolling } from '@/hooks/usePolling'
import { formatSpan, formatTimestamp, sentenceCase } from '@/utils/format'
import { actionLabel, rootCauseLabel } from '@/utils/incident'
import { VALIDATION_OUTCOME } from '@/utils/labels'
import { TriangleAlert } from 'lucide-react'

const PAGE_SIZE = 25
const FETCH_LIMIT = 200

const RESULTS = [
  { value: 'any', label: 'Any' },
  { value: 'succeeded', label: 'Succeeded' },
  { value: 'failed', label: 'Failed' },
  { value: 'none', label: 'Not executed' },
]

function resultOf(row) {
  if (row.action === 'escalate' || row.succeeded == null) return 'none'
  return row.succeeded ? 'succeeded' : 'failed'
}

function ResultCell({ row }) {
  if (row.action === 'escalate') return <Badge variant="warn">Escalated to an SRE</Badge>
  const result = resultOf(row)
  if (result === 'none') return <span className="text-muted-foreground">Not executed</span>
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {row.dry_run ? (
        <Badge variant="warn">Dry run, nothing changed</Badge>
      ) : (
        <StatusBadge status={result === 'succeeded' ? 'succeeded' : 'failed'} label={result === 'succeeded' ? 'Applied' : 'Failed'} />
      )}
      {result === 'succeeded' &&
        (row.validation_outcome ? (
          <StatusBadge status={row.validation_outcome === 'passed' ? 'passed' : 'failed'} label={row.validation_outcome === 'passed' ? 'Recovered' : (VALIDATION_OUTCOME[row.validation_outcome] ?? sentenceCase(row.validation_outcome))} />
        ) : (
          <Badge variant="neutral">Awaiting validation</Badge>
        ))}
    </div>
  )
}

const authLabel = (type) => (type === 'autonomous' ? 'Autonomous' : /sre|human|override/i.test(type ?? '') ? 'SRE authorized' : sentenceCase(type))

function ActionDrawer({ row, onClose }) {
  return (
    <Sheet open={Boolean(row)} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-full gap-0 overflow-y-auto sm:max-w-md">
        {row && (
          <>
            <SheetHeader className="border-b">
              <SheetTitle>{actionLabel(row.action)}</SheetTitle>
              <SheetDescription>
                {formatTimestamp(row.at)} on {row.app}
              </SheetDescription>
            </SheetHeader>
            <div className="space-y-5 p-4">
              <div>
                <h3 className="text-xs font-medium text-muted-foreground">Why Sentinel chose this</h3>
                <p className="mt-1 text-sm leading-6">{row.reason || 'No rationale recorded.'}</p>
              </div>
              <div>
                <h3 className="mb-1.5 text-xs font-medium text-muted-foreground">Confidence</h3>
                <ConfidenceMeter value={row.confidence} />
              </div>
              <div>
                <h3 className="mb-1.5 text-xs font-medium text-muted-foreground">Outcome</h3>
                <ResultCell row={row} />
              </div>
              <KeyValue
                items={[
                  { label: 'Incident', value: <Link to={`/incidents/${row.incident_id}`} className="font-mono text-xs underline underline-offset-2">{row.incident_id}</Link> },
                  { label: 'Service', value: row.app },
                  { label: 'Target', value: row.target, mono: true },
                  { label: 'Diagnosis', value: row.root_cause && rootCauseLabel(row.root_cause) },
                  { label: 'Authorization', value: authLabel(row.authorization_type) },
                  { label: 'Mode', value: row.action === 'escalate' ? null : row.dry_run ? 'Dry run, nothing changed' : 'Applied to the cluster' },
                  { label: 'Duration', value: row.duration_seconds != null && formatSpan(row.duration_seconds) },
                ]}
              />
              <Button asChild className="w-full">
                <Link to={`/incidents/${row.incident_id}`}>Open incident</Link>
              </Button>
            </div>
          </>
        )}
      </SheetContent>
    </Sheet>
  )
}

export default function ActionHistoryPage() {
  usePageTitle('Action ledger')
  const [action, setAction] = useState('any')
  const [auth, setAuth] = useState('any')
  const [result, setResult] = useState('any')
  const [query, setQuery] = useState('')
  const [page, setPage] = usePage(`${action}|${auth}|${result}|${query}`)
  const [actionTypes, setActionTypes] = useState([])
  const [selected, setSelected] = useState(null)

  useEffect(() => {
    listActionTypes().then(setActionTypes).catch(() => {})
  }, [])

  const { data, error, loading, refetch } = usePolling(() => listActions({ action: action === 'any' ? null : action, limit: FETCH_LIMIT }), { intervalMs: 8000, resetKey: action })

  const rows = useMemo(() => data?.actions ?? [], [data])
  const authTypes = useMemo(() => [...new Set(rows.map((r) => r.authorization_type).filter(Boolean))], [rows])
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return rows.filter((r) => {
      if (auth !== 'any' && r.authorization_type !== auth) return false
      if (result !== 'any' && resultOf(r) !== result) return false
      if (!q) return true
      return [r.incident_id, r.app, r.target, r.action, r.root_cause].filter(Boolean).some((v) => String(v).toLowerCase().includes(q))
    })
  }, [rows, auth, result, query])

  const pageRows = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)
  const hasFilter = action !== 'any' || auth !== 'any' || result !== 'any' || query

  function clear() {
    setAction('any')
    setAuth('any')
    setResult('any')
    setQuery('')
  }

  return (
    <div className="space-y-4">
      <PageHeader title="Action ledger" description="Every remediation Sentinel decided on, who or what authorized it, and how it turned out. The record of everything it has changed." />

      {error && data && (
        <Callout tone="warn" icon={TriangleAlert} title="Showing the last data received">
          {extractErrorMessage(error, 'Could not refresh the ledger.')} Retrying automatically.
        </Callout>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <SearchInput value={query} onChange={setQuery} placeholder="Search incident, service, target or cause" className="w-full sm:w-80" label="Search actions" />
        <FilterSelect label="Action" value={action} onChange={setAction} width="w-56" options={[{ value: 'any', label: 'All' }, ...[...actionTypes.filter((a) => a !== 'escalate'), 'escalate'].map((a) => ({ value: a, label: actionLabel(a) }))]} />
        <FilterSelect label="Authorization" value={auth} onChange={setAuth} width="w-52" options={[{ value: 'any', label: 'Any' }, ...authTypes.map((t) => ({ value: t, label: authLabel(t) }))]} />
        <FilterSelect label="Result" value={result} onChange={setResult} width="w-48" options={RESULTS} />
        {hasFilter && (
          <Button variant="ghost" size="sm" onClick={clear}>
            Clear filters
          </Button>
        )}
        <div className="ml-auto flex items-center gap-3">
          <span className="tnum text-xs text-muted-foreground" aria-live="polite">
            {filtered.length} of {rows.length}
            {rows.length >= FETCH_LIMIT ? ` (latest ${FETCH_LIMIT})` : ''}
          </span>
          <Pager page={page} pageSize={PAGE_SIZE} total={filtered.length} onPageChange={setPage} noun="actions" />
        </div>
      </div>

      <Card className="gap-0 overflow-hidden">
        {loading && !data ? (
          <SkeletonRows rows={8} />
        ) : !data ? (
          <ErrorState title="Couldn’t load the action ledger" message={extractErrorMessage(error, 'Sentinel did not respond.')} onRetry={refetch} />
        ) : filtered.length === 0 ? (
          <EmptyState
            icon={hasFilter ? SearchX : ListChecks}
            title={hasFilter ? 'No actions match these filters' : 'No actions recorded yet'}
            description={hasFilter ? 'Try a different action or result, or clear the filters.' : 'When Sentinel restarts, rolls back or scales something, or escalates instead, it is recorded here.'}
            action={hasFilter ? <Button variant="outline" size="sm" onClick={clear}>Clear filters</Button> : null}
          />
        ) : (
          <Table>
            <caption className="sr-only">Remediation actions, newest first</caption>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Time</TableHead>
                <TableHead>Action</TableHead>
                <TableHead className="hidden md:table-cell">Incident</TableHead>
                <TableHead className="hidden lg:table-cell">Diagnosis</TableHead>
                <TableHead>Authorization</TableHead>
                <TableHead>Result</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {pageRows.map((row, i) => (
                <TableRow key={`${row.incident_id}-${row.at}-${i}`} tabIndex={0} onClick={() => setSelected(row)} onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), setSelected(row))} className="cursor-pointer focus-visible:bg-muted/60 focus-visible:outline-none">
                  <TableCell className="text-muted-foreground">
                    <Timestamp value={row.at} />
                  </TableCell>
                  <TableCell>
                    <p className="font-medium">{actionLabel(row.action)}</p>
                    {row.target && <p className="font-mono text-xs text-muted-foreground">{row.target}</p>}
                  </TableCell>
                  <TableCell className="hidden md:table-cell">
                    <Link to={`/incidents/${row.incident_id}`} onClick={(e) => e.stopPropagation()} className="font-mono text-xs underline-offset-2 hover:underline">
                      {row.incident_id}
                    </Link>
                    <p className="text-xs text-muted-foreground">{row.app}</p>
                  </TableCell>
                  <TableCell className="hidden lg:table-cell">
                    {row.root_cause ? rootCauseLabel(row.root_cause) : '—'}
                    {row.confidence != null && <p className="tnum text-xs text-muted-foreground">{Math.round(row.confidence * 100)}% confidence</p>}
                  </TableCell>
                  <TableCell>
                    <Badge variant={row.authorization_type === 'autonomous' ? 'neutral' : 'info'}>{authLabel(row.authorization_type)}</Badge>
                  </TableCell>
                  <TableCell>
                    <ResultCell row={row} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Card>

      <ActionDrawer row={selected} onClose={() => setSelected(null)} />
    </div>
  )
}
