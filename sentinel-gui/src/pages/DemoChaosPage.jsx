import { useEffect, useState } from 'react'
import { extractErrorMessage } from '../api/client.js'
import { getChaosToken, getRunStatus, listScenarios, runScenario, setChaosToken } from '../api/chaosScenarios.js'
import AlertBanner from '../components/ui/AlertBanner.jsx'
import Button from '../components/ui/Button.jsx'
import Card from '../components/ui/Card.jsx'
import ConfirmDialog from '../components/ui/ConfirmDialog.jsx'
import EmptyState from '../components/ui/EmptyState.jsx'
import Field from '../components/ui/Field.jsx'
import { Skeleton } from '../components/ui/Loading.jsx'
import PageHeader from '../components/ui/PageHeader.jsx'
import StatusPill from '../components/ui/StatusPill.jsx'
import Tag from '../components/ui/Tag.jsx'
import { usePageTitle } from '../hooks/usePageTitle.js'

const TERMINAL = new Set(['Success', 'Failed', 'Cancelled', 'TimedOut'])

// Polls one SSM command until it reaches a terminal state, then stops. Kept in
// this file (not the generic usePolling) because it is specific to this shape
// and must stop on its own.
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
        if (!TERMINAL.has(result.status)) {
          timer = setTimeout(tick, 3000)
        }
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

function RunStatus({ run }) {
  const { data, error } = useCommandStatus(run.commandId)
  if (error) return <AlertBanner>{extractErrorMessage(error, 'Could not read run status.')}</AlertBanner>
  if (!data) return <Skeleton width={160} height={22} />
  return (
    <div className="stack" style={{ gap: 8 }}>
      <div className="cluster">
        <span className="muted">Status</span>
        <StatusPill status={data.status} />
      </div>
      {data.status_details && <div className="muted small">{data.status_details}</div>}
    </div>
  )
}

function TokenGate({ onSet }) {
  const [value, setValue] = useState('')

  function handleSubmit(event) {
    event.preventDefault()
    if (value) onSet(value)
  }

  return (
    <Card
      title="Enter the chaos admin token"
      description="This is the existing CHAOS_ADMIN_TOKEN shared secret (see sentinel-ai/app/routers/chaos_scenarios.py) — not your Sentinel admin login. It is kept in this tab's session only."
    >
      <form className="demo-token-form" onSubmit={handleSubmit}>
        <Field label="Chaos admin token">
          <input
            type="password"
            className="input"
            autoComplete="off"
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
        </Field>
        <Button type="submit" variant="primary" disabled={!value}>
          Continue
        </Button>
      </form>
    </Card>
  )
}

function ScenarioCard({ scenario, onRun, running }) {
  const [autoRollback, setAutoRollback] = useState(Boolean(scenario.auto_rollback_supported))
  const [confirming, setConfirming] = useState(false)

  function handleRun() {
    if (scenario.dangerous) {
      setConfirming(true)
      return
    }
    onRun(scenario.id, autoRollback)
  }

  return (
    <li className="demo-scenario">
      <div className="demo-scenario__header">
        <strong>{scenario.title}</strong>
        <Tag>{scenario.family}</Tag>
        {scenario.dangerous && <Tag tone="warn">Dangerous</Tag>}
      </div>
      <p className="muted small">{scenario.description}</p>
      <div className="demo-scenario__actions">
        {scenario.auto_rollback_supported && (
          <label className="checkbox">
            <input type="checkbox" checked={autoRollback} onChange={(e) => setAutoRollback(e.target.checked)} />
            Auto-rollback
          </label>
        )}
        <Button
          variant={scenario.dangerous ? 'caution' : 'secondary'}
          onClick={handleRun}
          busy={running}
          busyLabel="Running…"
        >
          Run scenario
        </Button>
      </div>

      <ConfirmDialog
        open={confirming}
        title={`Run “${scenario.title}”?`}
        confirmLabel="Run anyway"
        tone="caution"
        onConfirm={() => {
          setConfirming(false)
          onRun(scenario.id, autoRollback)
        }}
        onCancel={() => setConfirming(false)}
      >
        <p>This scenario is marked dangerous and may leave the incident open.</p>
      </ConfirmDialog>
    </li>
  )
}

export default function DemoChaosPage() {
  usePageTitle('Chaos scenarios')
  const [tokenSet, setTokenSet] = useState(Boolean(getChaosToken()))
  const [scenarios, setScenarios] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)
  const [runningId, setRunningId] = useState(null)
  const [activeRun, setActiveRun] = useState(null)

  useEffect(() => {
    if (!tokenSet) return
    setLoading(true)
    listScenarios()
      .then((data) => setScenarios(data.scenarios))
      .catch((err) => setError(extractErrorMessage(err, 'Could not load chaos scenarios.')))
      .finally(() => setLoading(false))
  }, [tokenSet])

  function handleTokenSet(value) {
    setChaosToken(value)
    setTokenSet(true)
  }

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
    <div className="page">
      <PageHeader
        title="Demo & chaos utilities"
        subtitle="Admin/demo tooling only — not a Sentinel capability."
      />

      <AlertBanner tone="warn" title="This triggers real faults">
        Scenarios here break things in the demo cluster on purpose, via the existing chaos-scenario runner, so you can show
        Sentinel responding.
      </AlertBanner>

      {!tokenSet ? (
        <TokenGate onSet={handleTokenSet} />
      ) : (
        <>
          {error && <AlertBanner>{error}</AlertBanner>}

          {activeRun && (
            <Card title={`Run in progress: ${activeRun.scenario}`}>
              <RunStatus run={activeRun} />
            </Card>
          )}

          <Card title="Scenarios" muted>
            {loading ? (
              <div className="stack">
                <Skeleton height={64} />
                <Skeleton height={64} />
              </div>
            ) : (scenarios ?? []).length === 0 ? (
              <EmptyState compact title="No scenarios available" description="The chaos runner returned no scenarios." />
            ) : (
              <ul className="demo-scenario-list">
                {scenarios.map((scenario) => (
                  <ScenarioCard key={scenario.id} scenario={scenario} onRun={handleRun} running={runningId === scenario.id} />
                ))}
              </ul>
            )}
          </Card>
        </>
      )}
    </div>
  )
}
