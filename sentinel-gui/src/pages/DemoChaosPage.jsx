import { useEffect, useState } from 'react'
import AlertBanner from '../components/AlertBanner.jsx'
import Spinner from '../components/Spinner.jsx'
import { extractErrorMessage } from '../api/client.js'
import { getChaosToken, getRunStatus, listScenarios, runScenario, setChaosToken } from '../api/chaosScenarios.js'

function RunStatus({ run }) {
  const { data, error } = usePolling(run.commandId)
  if (error) return <AlertBanner>{extractErrorMessage(error, 'Could not read run status.')}</AlertBanner>
  if (!data) return <Spinner label="Checking run status…" />
  return (
    <div className="demo-run-status">
      <div>
        Status: <strong>{data.status}</strong>
      </div>
      {data.status_details && <div className="muted small">{data.status_details}</div>}
    </div>
  )
}

// A tiny local hook (kept in this file — it is specific to one field shape
// and not reused elsewhere) rather than the generic usePolling, since this
// needs to stop polling once the SSM command reaches a terminal state.
function usePolling(commandId) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    let timer
    const TERMINAL = new Set(['Success', 'Failed', 'Cancelled', 'TimedOut'])

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

function TokenGate({ onSet }) {
  const [value, setValue] = useState('')
  return (
    <section className="card">
      <h2 className="card__title">Enter the chaos admin token</h2>
      <p className="muted">
        This is the existing <code>CHAOS_ADMIN_TOKEN</code> shared secret (see
        sentinel-ai/app/routers/chaos_scenarios.py) — not your Sentinel admin login. Kept in this tab's
        session only.
      </p>
      <div className="demo-token-form">
        <input
          type="password"
          className="select"
          placeholder="Chaos admin token"
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
        <button type="button" className="button button--primary" onClick={() => onSet(value)} disabled={!value}>
          Continue
        </button>
      </div>
    </section>
  )
}

function ScenarioCard({ scenario, onRun, running }) {
  const [autoRollback, setAutoRollback] = useState(Boolean(scenario.auto_rollback_supported))

  function handleRun() {
    if (scenario.dangerous) {
      const confirmed = window.confirm(
        `"${scenario.title}" is marked dangerous and may leave the incident open. Run it anyway?`
      )
      if (!confirmed) return
    }
    onRun(scenario.id, autoRollback)
  }

  return (
    <div className="demo-scenario-card">
      <div className="demo-scenario-card__header">
        <strong>{scenario.title}</strong>
        <span className="tag tag--muted">{scenario.family}</span>
        {scenario.dangerous && <span className="tag tag--warn">dangerous</span>}
      </div>
      <p className="muted small">{scenario.description}</p>
      <div className="demo-scenario-card__actions">
        {scenario.auto_rollback_supported && (
          <label className="checkbox-field">
            <input type="checkbox" checked={autoRollback} onChange={(e) => setAutoRollback(e.target.checked)} />
            Auto-rollback
          </label>
        )}
        <button type="button" className="button button--ghost" onClick={handleRun} disabled={running}>
          {running ? 'Running…' : 'Run scenario'}
        </button>
      </div>
    </div>
  )
}

export default function DemoChaosPage() {
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
      <div className="page__header">
        <div>
          <h1>Demo &amp; Chaos Utilities</h1>
          <p className="page__subtitle muted">
            Admin/demo tooling only — not a Sentinel capability. Triggers real faults in the demo cluster via the
            existing chaos-scenario runner.
          </p>
        </div>
      </div>

      {!tokenSet ? (
        <TokenGate onSet={handleTokenSet} />
      ) : (
        <>
          {error && <AlertBanner>{error}</AlertBanner>}

          {activeRun && (
            <section className="card">
              <h2 className="card__title">Run in progress: {activeRun.scenario}</h2>
              <RunStatus run={activeRun} />
            </section>
          )}

          <section className="card">
            <h2 className="card__title">Scenarios</h2>
            {loading ? (
              <Spinner label="Loading scenarios…" />
            ) : (
              <div className="demo-scenario-list">
                {(scenarios ?? []).map((scenario) => (
                  <ScenarioCard
                    key={scenario.id}
                    scenario={scenario}
                    onRun={handleRun}
                    running={runningId === scenario.id}
                  />
                ))}
              </div>
            )}
          </section>
        </>
      )}
    </div>
  )
}
