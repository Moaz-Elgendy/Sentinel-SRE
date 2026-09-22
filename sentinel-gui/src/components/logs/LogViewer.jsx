import { ArrowUpToLine, ChevronRight, Eraser, Pause, Play, ScrollText, SearchX } from 'lucide-react'
import { Fragment, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { extractErrorMessage } from '@/api/client'
import { CopyButton } from '@/components/sentinel/CopyButton'
import { FilterSelect, SearchInput } from '@/components/sentinel/FilterBar'
import { LiveIndicator } from '@/components/sentinel/LiveIndicator'
import { Callout, EmptyState, SkeletonRows } from '@/components/sentinel/States'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { useLogsStream } from '@/hooks/useLogsStream'
import { cn } from '@/lib/utils'
import { formatClockMs } from '@/utils/format'
import { TriangleAlert } from 'lucide-react'

const LEVELS = [
  { value: 'any', label: 'Any' },
  { value: 'DEBUG', label: 'Debug' },
  { value: 'INFO', label: 'Info' },
  { value: 'WARNING', label: 'Warning' },
  { value: 'ERROR', label: 'Error' },
  { value: 'CRITICAL', label: 'Critical' },
]

const LEVEL_STYLE = {
  DEBUG: 'text-muted-foreground',
  INFO: 'text-foreground/80',
  WARNING: 'border-warn-edge bg-warn-tint text-warn',
  ERROR: 'border-bad-edge bg-bad-tint text-bad',
  CRITICAL: 'border-bad-edge bg-bad-tint text-bad font-semibold',
}

// Fields already shown in the row itself; everything else is structured context.
const CORE_FIELDS = new Set(['name', 'message', 'timestamp', 'timestamp_epoch', 'level', 'taskName', 'service', 'incident_id'])

function stringify(value) {
  if (value == null) return 'null'
  return typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value)
}

function LogRow({ entry, expanded, onToggle }) {
  const extras = Object.entries(entry).filter(([key, value]) => !CORE_FIELDS.has(key) && value != null)
  const hasDetail = extras.length > 0
  return (
    <li className="border-b border-border/60 last:border-0">
      <div
        role={hasDetail ? 'button' : undefined}
        tabIndex={hasDetail ? 0 : undefined}
        aria-expanded={hasDetail ? expanded : undefined}
        onClick={hasDetail ? onToggle : undefined}
        onKeyDown={hasDetail ? (e) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), onToggle()) : undefined}
        className={cn(
          'grid grid-cols-[6.25rem_4.75rem_minmax(0,1fr)] items-baseline gap-x-3 px-3 py-1 font-mono text-xs leading-5 outline-none sm:grid-cols-[6.25rem_4.75rem_11rem_minmax(0,1fr)]',
          hasDetail && 'cursor-pointer hover:bg-muted/40 focus-visible:bg-muted/60'
        )}
      >
        <time className="tnum text-muted-foreground" dateTime={new Date(entry.timestamp_epoch * 1000).toISOString()}>
          {formatClockMs(entry.timestamp_epoch)}
        </time>
        <span className={cn('w-fit rounded border border-transparent px-1 text-[11px] leading-4', LEVEL_STYLE[entry.level] ?? 'text-muted-foreground')}>{entry.level ?? '—'}</span>
        <span className="hidden truncate text-muted-foreground sm:block" title={entry.name}>
          {entry.name}
        </span>
        <span className="flex min-w-0 items-baseline gap-2">
          {hasDetail && <ChevronRight aria-hidden="true" className={cn('relative top-0.5 size-3 shrink-0 text-muted-foreground transition-transform', expanded && 'rotate-90')} />}
          <span className={cn('min-w-0 font-sans text-[13px] break-words', !expanded && 'line-clamp-2')}>{entry.message}</span>
          {entry.incident_id && (
            <Link to={`/incidents/${entry.incident_id}`} onClick={(e) => e.stopPropagation()} className="shrink-0 text-muted-foreground underline-offset-2 hover:text-foreground hover:underline">
              {entry.incident_id}
            </Link>
          )}
        </span>
      </div>
      {expanded && hasDetail && (
        <div className="mx-3 mb-2 ml-3 rounded-md border bg-muted/30 p-2.5 sm:ml-[calc(6.25rem+4.75rem+1.5rem)]">
          <dl className="grid gap-x-4 gap-y-1 text-xs sm:grid-cols-[auto_minmax(0,1fr)]">
            {extras.map(([key, value]) => (
              <div key={key} className="contents">
                <dt className="text-muted-foreground">{key}</dt>
                <dd className="font-mono break-words whitespace-pre-wrap">{stringify(value)}</dd>
              </div>
            ))}
          </dl>
          <div className="mt-2 flex justify-end">
            <CopyButton value={JSON.stringify(entry, null, 2)} label="Copy line as JSON" />
          </div>
        </div>
      )}
    </li>
  )
}

/**
 * Sentinel's own backend logs (startup, evidence collection, RCA, policy,
 * remediation, escalation, errors) — not the monitored application's logs.
 * An initial query of persisted lines, then a live SSE tail. `incidentId` pins
 * the viewer to one incident (used on the incident page).
 */
export function LogViewer({ incidentId: pinnedIncidentId, className, height = 'h-[32rem]' }) {
  const [level, setLevel] = useState('any')
  const [component, setComponent] = useState('')
  const [incidentFilter, setIncidentFilter] = useState('')
  const [query, setQuery] = useState('')
  const [follow, setFollow] = useState(true)
  const [expanded, setExpanded] = useState(() => new Set())
  const scrollRef = useRef(null)

  const incidentId = pinnedIncidentId ?? (incidentFilter || undefined)
  const { lines, loading, error, connected, paused, setPaused, pendingCount, clearView } = useLogsStream({
    level: level === 'any' ? undefined : level,
    component: component || undefined,
    incidentId,
    q: query || undefined,
  })

  const hasFilter = Boolean(level !== 'any' || component || (!pinnedIncidentId && incidentFilter) || query)

  // Newest entries render at the TOP, so "following" means staying
  // pinned to scrollTop 0 as new lines arrive — the inverse of a
  // classic `tail -f`. Scrolling down away from the top turns
  // following off.
  useEffect(() => {
    const el = scrollRef.current
    if (follow && el) el.scrollTop = 0
  }, [lines, follow, loading])

  function onScroll() {
    const el = scrollRef.current
    if (!el) return
    const atTop = el.scrollTop < 48
    setFollow((current) => (current === atTop ? current : atTop))
  }

  function clearFilters() {
    setLevel('any')
    setComponent('')
    setIncidentFilter('')
    setQuery('')
  }

  function toggle(key) {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  return (
    <div className={cn('overflow-hidden rounded-lg border bg-card', className)}>
      <div role="search" className="flex flex-wrap items-center gap-2 border-b p-2.5">
        <SearchInput value={query} onChange={setQuery} placeholder="Search log messages" label="Search log messages" className="w-full sm:w-64" />
        <FilterSelect label="Level" value={level} onChange={setLevel} options={LEVELS} width="w-36" />
        <Input value={component} onChange={(e) => setComponent(e.target.value)} placeholder="Component, e.g. rca" aria-label="Filter by component" className="h-8 w-40" />
        {!pinnedIncidentId && <Input value={incidentFilter} onChange={(e) => setIncidentFilter(e.target.value)} placeholder="Incident ID" aria-label="Filter by incident ID" className="h-8 w-36 font-mono text-xs" />}
        {hasFilter && (
          <Button variant="ghost" size="sm" onClick={clearFilters}>
            Clear filters
          </Button>
        )}
        <div className="ml-auto flex items-center gap-2">
          {connected && !paused && <LiveIndicator />}
          <Button variant={paused ? 'default' : 'outline'} size="sm" onClick={() => setPaused((p) => !p)}>
            {paused ? <Play /> : <Pause />}
            {paused ? `Resume${pendingCount ? ` (${pendingCount} new)` : ''}` : 'Pause'}
          </Button>
          <Button variant="ghost" size="sm" onClick={clearView} title="Clears this view only. The persisted log file is never touched.">
            <Eraser /> Clear view
          </Button>
        </div>
      </div>

      {error && (
        <div className="border-b p-2.5">
          <Callout tone="warn" icon={TriangleAlert} title="Couldn’t load persisted logs">
            {extractErrorMessage(error, 'Sentinel did not respond.')} The live tail still works while connected.
          </Callout>
        </div>
      )}

      <div className="relative">
        <div ref={scrollRef} onScroll={onScroll} className={cn('overflow-y-auto', height)}>
          {loading && lines.length === 0 ? (
            <SkeletonRows rows={8} />
          ) : lines.length === 0 ? (
            hasFilter ? (
              <EmptyState icon={SearchX} title="No log lines match these filters" description="Try a different level, component or search term." action={<Button variant="outline" size="sm" onClick={clearFilters}>Clear filters</Button>} />
            ) : (
              <EmptyState icon={ScrollText} title="No logs yet" description="Sentinel’s own logs appear here as it runs. Clearing the view never deletes the persisted log." />
            )
          ) : (
            <ul>
              {lines.map((entry, index) => {
                const key = `${entry.timestamp_epoch}-${index}`
                // Rows show clock time only, so mark each change of day; otherwise lines
                // that span several days read as if they were out of order.
                const day = new Date(entry.timestamp_epoch * 1000).toDateString()
                const previous = lines[index - 1]
                const newDay = !previous || new Date(previous.timestamp_epoch * 1000).toDateString() !== day
                return (
                  <Fragment key={key}>
                    {newDay && (
                      <li role="separator" className="sticky top-0 z-10 border-b bg-muted/80 px-3 py-1 text-[11px] font-medium text-muted-foreground backdrop-blur">
                        {new Date(entry.timestamp_epoch * 1000).toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' })}
                      </li>
                    )}
                    <LogRow entry={entry} expanded={expanded.has(key)} onToggle={() => toggle(key)} />
                  </Fragment>
                )
              })}
            </ul>
          )}
        </div>
        {!follow && lines.length > 0 && (
          <Button size="sm" variant="secondary" className="absolute top-3 right-4 shadow-md" onClick={() => setFollow(true)}>
            <ArrowUpToLine /> Jump to latest
          </Button>
        )}
      </div>
      <div className="tnum flex items-center justify-between border-t px-3 py-1.5 text-xs text-muted-foreground">
        <span>{lines.length} line{lines.length === 1 ? '' : 's'}{paused && pendingCount ? `, ${pendingCount} waiting` : ''}</span>
        <span>{paused ? 'Paused' : follow ? 'Following the tail' : 'Scrolled back'}</span>
      </div>
    </div>
  )
}
