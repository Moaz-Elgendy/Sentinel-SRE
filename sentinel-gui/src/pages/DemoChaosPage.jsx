import { ChevronDown, ChevronRight, FlaskConical, KeyRound, LoaderCircle, OctagonAlert, Play, RotateCcw, TriangleAlert } from 'lucide-react'
import { useEffect, useState } from 'react'
import { extractErrorMessage } from '@/api/client'
import { getChaosToken, getRunStatus, listScenarios, runScenario, setChaosToken } from '@/api/chaosScenarios'
import { ConfirmDialog } from '@/components/sentinel/ConfirmDialog'
import { PageHeader } from '@/components/sentinel/PageHeader'
import { Panel } from '@/components/sentinel/Panel'
import { Callout, EmptyState, SkeletonRows } from '@/components/sentinel/States'
import { StatusBadge } from '@/components/sentinel/StatusBadge'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { usePageTitle } from '@/hooks/usePageTitle'
import { sentenceCase } from '@/utils/format'

const TERMINAL = new Set(['Success', 'Failed', 'Cancelled', 'TimedOut'])

// Polls one SSM command until it reaches a terminal state, then stops on its own.
function useCommandStatus(commandId) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  useEffect(() => {
    let cancelled = false
    let timer
    async function tick() {
      try {
        const result = await getRunStatus(commandId)
        if (cancelled) return
        setData(result)
        setError(null)
        if (!TERMINAL.has(result.status)) timer = setTimeout(tick, 3000)
      } catch (err) {
        if (!cancelled) setError(err)
      }
    }
    tick()
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [commandId])
  return { data, error }
}

// Non-2xx from the chaos API, a stuck deployment, a missing token, etc. all
// surface here as the script's own `FAILED: ...` line — the SSM top-level
// `status`/`status_details` only say "the shell exited non-zero", never why.
// Without this, a failed run is undebuggable from the GUI: the operator sees
// "Failed" and nothing else, even though the script prints exactly what went
// wrong on stderr (or stdout, since most of incident-scenarios.sh's own
// diagnostics go there).
const FAILED_STATUSES = new Set(['Failed', 'TimedOut', 'Cancelled'])

function CommandOutput({ data }) {
  const [open, setOpen] = useState(FAILED_STATUSES.has(data.status))
  const stderr = (data.stderr ?? '').trim()
  const stdout = (data.stdout ?? '').trim()
  if (!stderr && !stdout) return null
  return (
    <div className="border-t pt-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1 text-xs font-medium text-muted-foreground hover:text-foreground"
      >
        {open ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
        Command output
        {typeof data.response_code === 'number' && data.response_code !== 0 && (
          <span className="font-mono text-[10px] text-destructive">exit {data.response_code}</span>
        )}
      </button>
      {open && (
        <div className="mt-2 space-y-2">
          {stderr && (
            <div>
              <p className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">stderr</p>
              <pre className="max-h-72 overflow-auto rounded-md bg-muted p-2 font-mono text-xs whitespace-pre-wrap text-destructive">{stderr}</pre>
            </div>
          )}
          {stdout && (
            <div>
              <p className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">stdout</p>
              <pre className="max-h-72 overflow-auto rounded-md bg-muted p-2 font-mono text-xs whitespace-pre-wrap">{stdout}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function RunStatus({ run }) {
  const { data, error } = useCommandStatus(run.commandId)
  if (error) return <Callout tone="bad">{extractErrorMessage(error, 'Could not read run status.')}</Callout>
  if (!data) return <SkeletonRows rows={1} className="p-0" />
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 text-sm">
        <span className="text-muted-foreground">Status</span>
        <StatusBadge status={data.status} />
      </div>
      {data.status_details && <p className="text-xs text-muted-foreground">{data.status_details}</p>}
      <CommandOutput data={data} />
    </div>
  )
}

function TokenGate({ onSet }) {
  const [value, setValue] = useState('')
  return (
    <Panel title="Enter the chaos admin token" icon={KeyRound} description="This is the existing CHAOS_ADMIN_TOKEN shared secret, not your Sentinel admin login. It is kept in this tab’s session only.">
      <form
        className="flex max-w-md flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault()
          if (value) onSet(value)
        }}
      >
        <div className="space-y-1.5">
          <Label htmlFor="chaos-token">Chaos admin token</Label>
          <Input id="chaos-token" type="password" autoComplete="off" value={value} onChange={(e) => setValue(e.target.value)} />
        </div>
        <Button type="submit" disabled={!value} className="self-start">
          Continue
        </Button>
      </form>
    </Panel>
  )
}

function ScenarioRow({ scenario, onRun, running }) {
  const [autoRollback, setAutoRollback] = useState(Boolean(scenario.auto_rollback_supported))
  const [confirming, setConfirming] = useState(false)
  const id = `rollback-${scenario.id}`
  return (
    <li className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3 px-4 py-3.5">
      <div className="min-w-0 flex-1 basis-80">
        <div className="flex flex-wrap items-center gap-2">
          <p className="text-sm font-medium">{scenario.title}</p>
          <Badge variant="secondary">{sentenceCase(scenario.family)}</Badge>
          {scenario.dangerous && (
            <Badge variant="warn">
              <TriangleAlert /> Dangerous
            </Badge>
          )}
        </div>
        <p className="mt-1 text-xs text-muted-foreground">{scenario.description}</p>
      </div>
      <div className="flex items-center gap-4">
        {scenario.auto_rollback_supported && (
          <div className="flex items-center gap-2">
            <Checkbox id={id} checked={autoRollback} onCheckedChange={(v) => setAutoRollback(v === true)} />
            <Label htmlFor={id} className="text-xs font-normal">
              Auto-rollback
            </Label>
          </div>
        )}
        <Button variant="outline" size="sm" disabled={running} onClick={() => (scenario.dangerous ? setConfirming(true) : onRun(scenario.id, autoRollback))}>
          {running ? <LoaderCircle className="animate-spin" /> : <Play />} {running ? 'Starting…' : 'Run scenario'}
        </Button>
      </div>
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title={`Run “${scenario.title}”?`}
        description="This scenario is marked dangerous and may leave the incident open."
        confirmLabel="Run anyway"
        onConfirm={() => {
          setConfirming(false)
          onRun(scenario.id, autoRollback)
        }}
      />
    </li>
  )
}

export default function DemoChaosPage() {
  usePageTitle('Chaos scenarios')
  const [tokenSet, setTokenSet] = useState(Boolean(getChaosToken()))
  const [scenarios, setScenarios] = useState(null)
  const [error, setError] = useState(null)
  const [runningId, setRunningId] = useState(null)
  const [activeRun, setActiveRun] = useState(null)

  useEffect(() => {
    if (!tokenSet) return
    listScenarios()
      .then((data) => setScenarios(data.scenarios.filter((scenario) => !scenario.recovery)))
      .catch((err) => {
        setScenarios([])
        setError(extractErrorMessage(err, 'Could not load chaos scenarios.'))
      })
  }, [tokenSet])
  const loading = tokenSet && scenarios === null

  async function handleRun(scenarioId, autoRollback) {
    setRunningId(scenarioId)
    setError(null)
    try {
      const result = await runScenario(scenarioId, { autoRollback })
      setActiveRun({ scenario: scenarioId, commandId: result.command_id })
    } catch (err) {
      setError(extractErrorMessage(err, 'Could not start the scenario.'))
    } finally {
      setRunningId(null)
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader title="Chaos scenarios" description="Demo and admin tooling only. This is not a Sentinel capability." />
      <Callout tone="warn" icon={OctagonAlert} title="This triggers real faults">
        Scenarios here break things in the demo cluster on purpose, via the existing chaos-scenario runner, so you can show Sentinel responding.
      </Callout>
      {!tokenSet ? (
        <TokenGate
          onSet={(value) => {
            setChaosToken(value)
            setTokenSet(true)
          }}
        />
      ) : (
        <>
          {error && <Callout tone="bad">{error}</Callout>}
          {activeRun && (
            <Panel title={`Run in progress: ${activeRun.scenario}`}>
              <RunStatus run={activeRun} />
            </Panel>
          )}
          <Panel title="Recovery" icon={RotateCcw}>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <p className="max-w-2xl text-sm text-muted-foreground">
                Clear active chaos faults and recover the demo cluster if a scenario left a service down.
              </p>
              <Button
                variant="destructive"
                disabled={Boolean(runningId)}
                onClick={() => handleRun('reset-all', false)}
              >
                {runningId === 'reset-all' ? <LoaderCircle className="animate-spin" /> : <RotateCcw />}
                {runningId === 'reset-all' ? 'Recovering…' : 'Reset active scenarios'}
              </Button>
            </div>
          </Panel>
          <Panel title="Scenarios" flush>
            {loading ? (
              <SkeletonRows rows={2} />
            ) : (scenarios ?? []).length === 0 ? (
              <EmptyState compact icon={FlaskConical} title="No scenarios available" description="The chaos runner returned no scenarios." />
            ) : (
              <ul className="divide-y">
                {scenarios.map((scenario) => (
                  <ScenarioRow key={scenario.id} scenario={scenario} onRun={handleRun} running={runningId === scenario.id} />
                ))}
              </ul>
            )}
          </Panel>
        </>
      )}
    </div>
  )
}
