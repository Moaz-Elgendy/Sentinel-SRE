import { CircleCheck, Lock, TriangleAlert } from 'lucide-react'
import { NavLink, Outlet } from 'react-router-dom'
import { LoaderCircle } from 'lucide-react'
import { CONFIG_TABS } from '@/components/layout/nav'
import { Panel } from '@/components/sentinel/Panel'
import { PageHeader } from '@/components/sentinel/PageHeader'
import { Callout, ErrorState, SkeletonRows } from '@/components/sentinel/States'
import { Timestamp } from '@/components/sentinel/Timestamp'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Textarea } from '@/components/ui/textarea'
import { cn } from '@/lib/utils'
import { sentenceCase } from '@/utils/format'

/** Layout for the whole Configuration area: one header, six tabs, the active page below. */
export function ConfigLayout() {
  return (
    <div className="space-y-4">
      <PageHeader title="Guardrails and configuration" description="What Sentinel may do, how it diagnoses, and how it connects. Every change is reviewed first, takes effect immediately, and is recorded." />
      <nav aria-label="Configuration sections" className="-mx-1 overflow-x-auto border-b">
        <ul className="flex min-w-max gap-1 px-1">
          {CONFIG_TABS.map((tab) => (
            <li key={tab.to}>
              <NavLink
                to={tab.to}
                title={tab.description}
                className={({ isActive }) =>
                  cn(
                    'relative inline-flex h-9 items-center rounded-md px-3 text-sm font-medium outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring',
                    isActive ? 'text-foreground after:absolute after:inset-x-0 after:-bottom-px after:h-0.5 after:bg-foreground' : 'text-muted-foreground hover:text-foreground'
                  )
                }
              >
                {tab.label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
      <Outlet />
    </div>
  )
}

function formatChangeValue(value) {
  if (value == null) return '—'
  if (Array.isArray(value)) return value.join(', ') || '—'
  const text = String(value)
  return text === '' ? '(default)' : text
}

/** Step 2 of every change: the exact before/after diff, the backend's own warnings, an audit reason, an explicit confirm. */
function ReviewDialog({ editor }) {
  const { preview } = editor
  const hasErrors = Boolean(preview?.errors?.length)
  const warnings = preview && !hasErrors ? preview.diff.filter((d) => d.warning) : []
  return (
    <Dialog open={Boolean(preview)} onOpenChange={(open) => !open && !editor.applying && editor.cancelReview()}>
      <DialogContent className="sm:max-w-2xl">
        {preview && (
          <>
            <DialogHeader>
              <DialogTitle>{hasErrors ? 'These changes can’t be applied' : 'Review changes'}</DialogTitle>
              <DialogDescription>{hasErrors ? 'Sentinel validated your edits and rejected them. Nothing was changed.' : 'Nothing is saved until you confirm. Applied changes take effect immediately and are recorded in the audit trail.'}</DialogDescription>
            </DialogHeader>
            {hasErrors ? (
              <Callout tone="bad" icon={TriangleAlert} title="Validation failed">
                <ul className="list-inside list-disc">
                  {preview.errors.map((err) => (
                    <li key={err}>{err}</li>
                  ))}
                </ul>
              </Callout>
            ) : (
              <div className="space-y-3">
                <div className="overflow-hidden rounded-md border">
                  <Table>
                    <caption className="sr-only">Pending changes</caption>
                    <TableHeader>
                      <TableRow className="hover:bg-transparent">
                        <TableHead>Setting</TableHead>
                        <TableHead>Current</TableHead>
                        <TableHead>New</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {preview.diff.map((d) => (
                        <TableRow key={d.field}>
                          <TableCell>{sentenceCase(d.field)}</TableCell>
                          <TableCell className="font-mono text-xs text-muted-foreground line-through decoration-muted-foreground/40">{formatChangeValue(d.old_value)}</TableCell>
                          <TableCell className="font-mono text-xs font-medium">{formatChangeValue(d.new_value)}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
                {warnings.map((d) => (
                  <Callout key={d.field} tone="warn" icon={TriangleAlert}>
                    {d.warning}
                  </Callout>
                ))}
                <div className="space-y-1.5">
                  <Label htmlFor="change-reason">Reason</Label>
                  <Textarea id="change-reason" rows={2} value={editor.reason} onChange={(e) => editor.setReason(e.target.value)} placeholder="Optional, but recommended: it is stored with the change." />
                </div>
              </div>
            )}
            {editor.actionError && <Callout tone="bad">{editor.actionError}</Callout>}
            <DialogFooter>
              <Button variant="outline" onClick={editor.cancelReview} disabled={editor.applying}>
                {hasErrors ? 'Back to editing' : 'Cancel'}
              </Button>
              {!hasErrors && (
                <Button onClick={editor.apply} disabled={editor.applying}>
                  {editor.applying && <LoaderCircle className="animate-spin" />} Confirm and apply
                </Button>
              )}
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}

/** Floating save bar: only present while there is something to save. */
function ActionBar({ editor }) {
  if (editor.changeCount === 0) return null
  return (
    <div className="sticky bottom-4 z-20 flex justify-center">
      <div role="group" aria-label="Configuration changes" className="flex items-center gap-4 rounded-lg border bg-popover px-4 py-2.5 shadow-lg">
        <p className="text-sm" aria-live="polite">
          <span className="tnum mr-1.5 rounded bg-info-tint px-1.5 py-0.5 text-xs font-medium text-info">{editor.changeCount}</span>
          unsaved {editor.changeCount === 1 ? 'change' : 'changes'}
        </p>
        <div className="flex gap-2">
          <Button variant="ghost" size="sm" onClick={editor.discard} disabled={editor.reviewing}>
            Discard
          </Button>
          <Button size="sm" onClick={() => editor.review()} disabled={editor.reviewing}>
            {editor.reviewing && <LoaderCircle className="animate-spin" />} Review changes
          </Button>
        </div>
      </div>
    </div>
  )
}

/**
 * The shared frame of every configuration page: loading, load error, the
 * "applied" confirmation, the review dialog and the save bar. A page supplies
 * its settings as children and gets identical behaviour to the other five.
 */
export function ConfigPage({ editor, description, children, noActionBar = false, appliedMessage = 'Configuration applied. Sentinel is using the new values now.' }) {
  const { data } = editor
  if (editor.loading) return <SkeletonRows rows={5} />
  if (editor.loadError) return <div className="rounded-lg border bg-card"><ErrorState title="Couldn’t load this configuration" message={editor.loadError} onRetry={editor.retryLoad} /></div>
  if (!data || !editor.draft) return null

  return (
    <div className="space-y-4">
      <p className="max-w-3xl text-sm text-muted-foreground">
        {description}
        {data.last_changed_at && (
          <>
            {' '}
            Last changed <Timestamp value={data.last_changed_at} /> by {data.last_changed_by}.
          </>
        )}
      </p>
      {editor.justApplied && !editor.preview && (
        <Callout tone="ok" icon={CircleCheck}>
          {appliedMessage}
        </Callout>
      )}
      {editor.actionError && !editor.preview && <Callout tone="bad">{editor.actionError}</Callout>}
      {children}
      {!noActionBar && <ActionBar editor={editor} />}
      <ReviewDialog editor={editor} />
    </div>
  )
}

/** A group of related settings. */
export function SettingsSection({ title, description, children }) {
  return (
    <Panel title={title} description={description} flush>
      <div className="divide-y">{children}</div>
    </Panel>
  )
}

/** One setting: what it is on the left, its control on the right. */
export function SettingRow({ label, hint, modified, htmlFor, children }) {
  return (
    <div className="grid gap-x-8 gap-y-2 px-4 py-3 md:grid-cols-[minmax(0,24rem)_minmax(0,26rem)] md:items-start">
      <div>
        <Label htmlFor={htmlFor} className="text-sm font-medium">
          {label}
          {modified && <Badge variant="info" className="ml-2">Edited</Badge>}
        </Label>
        {hint && <p className="mt-0.5 text-xs text-muted-foreground">{hint}</p>}
      </div>
      <div>{children}</div>
    </div>
  )
}

/** Read-only settings are labelled as such, never faked as disabled inputs. */
export function ReadOnlyPanel({ title, description, children }) {
  return (
    <Panel title={title} description={description} actions={<Badge variant="outline"><Lock /> Read-only</Badge>}>
      {children}
    </Panel>
  )
}
