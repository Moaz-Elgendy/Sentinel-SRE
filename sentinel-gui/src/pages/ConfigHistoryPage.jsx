import { ChevronRight, History, Undo2 } from 'lucide-react'
import { useCallback, useEffect, useId, useState } from 'react'
import { extractErrorMessage } from '@/api/client'
import { getConfigHistory, restoreConfigChange } from '@/api/config'
import { ConfirmDialog } from '@/components/sentinel/ConfirmDialog'
import { FilterSelect, SearchInput } from '@/components/sentinel/FilterBar'
import { Callout, EmptyState, ErrorState, SkeletonRows } from '@/components/sentinel/States'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { usePageTitle } from '@/hooks/usePageTitle'
import { notify } from '@/lib/notify'
import { cn } from '@/lib/utils'
import { formatTimestamp, sentenceCase } from '@/utils/format'

// Registration order; a new admin category needs one line here to appear in the filter.
const CATEGORIES = ['policy', 'rca', 'remediation', 'ai', 'monitoring']
const CATEGORY_LABEL = { policy: 'Policies', rca: 'Diagnosis', remediation: 'Remediation', ai: 'AI reasoning', monitoring: 'Monitoring' }

const formatValue = (value) => (Array.isArray(value) ? value.join(', ') || '—' : String(value))

function ChangeRow({ entry, onRestored }) {
  const bodyId = useId()
  const [expanded, setExpanded] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [restoring, setRestoring] = useState(false)
  const [error, setError] = useState(null)
  const fields = entry.changes.map((c) => c.field).join(', ')

  async function restore() {
    setRestoring(true)
    setError(null)
    try {
      await restoreConfigChange(entry.id)
      setConfirming(false)
      notify.success(`Restored ${fields} to its previous value`)
      onRestored()
    } catch (err) {
      setConfirming(false)
      setError(extractErrorMessage(err, 'Could not restore this change.'))
    } finally {
      setRestoring(false)
    }
  }

  return (
    <li className="border-b last:border-0">
      <button type="button" onClick={() => setExpanded((v) => !v)} aria-expanded={expanded} aria-controls={bodyId} className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5 text-left outline-none hover:bg-muted/40 focus-visible:bg-muted/60">
        <ChevronRight aria-hidden="true" className={cn('size-3.5 shrink-0 text-muted-foreground transition-transform', expanded && 'rotate-90')} />
        <span className="tnum w-44 shrink-0 font-mono text-xs text-muted-foreground">{formatTimestamp(entry.created_at)}</span>
        <span className="w-28 shrink-0 truncate text-sm">{entry.admin_id}</span>
        <Badge variant="secondary">{CATEGORY_LABEL[entry.category] ?? sentenceCase(entry.category)}</Badge>
        <span className="min-w-0 flex-1 truncate font-mono text-xs">{fields}</span>
        {entry.restores_change_id && <Badge variant="info">Restore</Badge>}
        <Badge variant={entry.status === 'applied' ? 'ok' : 'bad'}>{sentenceCase(entry.status)}</Badge>
      </button>
      {expanded && (
        <div id={bodyId} className="space-y-3 border-t bg-muted/20 px-4 py-3 pl-11">
          {entry.reason && (
            <p className="text-sm">
              <span className="text-muted-foreground">Reason:</span> “{entry.reason}”
            </p>
          )}
          {entry.detail && <p className="text-sm text-muted-foreground">{entry.detail}</p>}
          <div className="overflow-hidden rounded-md border bg-card">
            <Table>
              <caption className="sr-only">Fields changed</caption>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead>Setting</TableHead>
                  <TableHead>Before</TableHead>
                  <TableHead>After</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {entry.changes.map((c) => (
                  <TableRow key={c.field}>
                    <TableCell>{sentenceCase(c.field)}</TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground line-through decoration-muted-foreground/40">{formatValue(c.old_value)}</TableCell>
                    <TableCell className="font-mono text-xs font-medium">{formatValue(c.new_value)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
          {error && <Callout tone="bad">{error}</Callout>}
          {entry.status === 'applied' && (
            <Button variant="outline" size="sm" onClick={() => setConfirming(true)}>
              <Undo2 /> Restore this change
            </Button>
          )}
        </div>
      )}
      <ConfirmDialog open={confirming} onOpenChange={setConfirming} title="Restore this change?" confirmLabel="Restore change" busy={restoring} onConfirm={restore}
        description={`This reverts ${fields} to the previous value${entry.changes.length > 1 ? 's' : ''}. It is applied as a new, audited change; the history is never rewritten.`} />
    </li>
  )
}

export default function ConfigHistoryPage() {
  usePageTitle('Configuration history')
  const [category, setCategory] = useState('all')
  const [query, setQuery] = useState('')
  const [history, setHistory] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  // Only the first load shows a skeleton; afterwards the list stays while a filter change or restore refreshes it.
  const refetch = useCallback(
    () =>
      getConfigHistory({ category: category === 'all' ? null : category })
        .then((data) => {
          setHistory(data)
          setError(null)
        })
        .catch((err) => setError(err))
        .finally(() => setLoading(false)),
    [category]
  )
  useEffect(() => {
    refetch()
  }, [refetch])

  const needle = query.trim().toLowerCase()
  const entries = (history ?? []).filter((entry) => !needle || [entry.admin_id, entry.reason, ...entry.changes.map((c) => c.field)].filter(Boolean).some((v) => String(v).toLowerCase().includes(needle)))

  return (
    <div className="space-y-4">
      <p className="max-w-3xl text-sm text-muted-foreground">Every change made through these pages: who, when, what, and why. Any applied change can be restored, and a restore is itself recorded.</p>
      {error && history && <Callout tone="warn" title="Showing the last data received">{extractErrorMessage(error, 'Could not refresh configuration history.')}</Callout>}
      <div className="flex flex-wrap items-center gap-2">
        <SearchInput value={query} onChange={setQuery} placeholder="Search admin, setting or reason" className="w-full sm:w-80" label="Search configuration history" />
        <FilterSelect label="Category" value={category} onChange={setCategory} width="w-56" options={[{ value: 'all', label: 'All' }, ...CATEGORIES.map((c) => ({ value: c, label: CATEGORY_LABEL[c] }))]} />
        <span className="tnum ml-auto text-xs text-muted-foreground" aria-live="polite">
          {entries.length} {entries.length === 1 ? 'change' : 'changes'}
        </span>
      </div>
      <Card className="gap-0 overflow-hidden">
        {loading && !history ? (
          <SkeletonRows rows={5} />
        ) : !history ? (
          <ErrorState title="Couldn’t load configuration history" message={extractErrorMessage(error, 'Sentinel did not respond.')} onRetry={() => { setLoading(true); refetch() }} />
        ) : entries.length === 0 ? (
          <EmptyState icon={History} title={needle ? 'No changes match your search' : category !== 'all' ? `No ${CATEGORY_LABEL[category]} changes yet` : 'No configuration changes yet'} description="Changes made on the other configuration tabs appear here with their reason and full before/after values." />
        ) : (
          <ul>{entries.map((entry) => <ChangeRow key={entry.id} entry={entry} onRestored={refetch} />)}</ul>
        )}
      </Card>
    </div>
  )
}
