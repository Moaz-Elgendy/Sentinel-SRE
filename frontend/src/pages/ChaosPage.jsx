import { useEffect, useMemo, useState } from 'react'
import {
  extractChaosError,
  getChaosRun,
  listChaosScenarios,
  runChaosScenario,
} from '../api/chaosScenarios'
import AlertBanner from '../components/AlertBanner'
import Spinner from '../components/Spinner'

const TOKEN_KEY = 'sentinel_chaos_token'
const TERMINAL_STATUSES = new Set(['Success', 'Failed', 'Cancelled', 'TimedOut', 'Cancelling'])

export default function ChaosPage() {
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_KEY) ?? '')
  const [namespace, setNamespace] = useState('citizen-portal')
  const [autoRollback, setAutoRollback] = useState(false)
  const [data, setData] = useState(null)
  const [run, setRun] = useState(null)
  const [loading, setLoading] = useState(false)
  const [runningId, setRunningId] = useState('')
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  const scenarios = useMemo(() => data?.scenarios ?? [], [data])

  useEffect(() => {
    if (token) {
      localStorage.setItem(TOKEN_KEY, token)
    } else {
      localStorage.removeItem(TOKEN_KEY)
    }
  }, [token])

  useEffect(() => {
    if (!run?.command_id || !token || TERMINAL_STATUSES.has(run.status)) return

    const interval = window.setInterval(async () => {
      try {
        const next = await getChaosRun(run.command_id, token)
        setRun(next)
      } catch (err) {
        setError(extractChaosError(err))
      }
    }, 5000)

    return () => window.clearInterval(interval)
  }, [run?.command_id, run?.status, token])

  async function loadScenarios(event) {
    event?.preventDefault()
    setError('')
    setSuccess('')
    setLoading(true)
    try {
      setData(await listChaosScenarios(token))
      setSuccess('Chaos runner connected.')
    } catch (err) {
      setError(extractChaosError(err))
    } finally {
      setLoading(false)
    }
  }

  async function handleRun(id) {
    setError('')
    setSuccess('')
    setRunningId(id)
    try {
      const started = await runChaosScenario(id, { token, namespace, autoRollback })
      setRun({ ...started, status: 'Pending' })
      setSuccess(`Started ${id}.`)
    } catch (err) {
      setError(extractChaosError(err))
    } finally {
      setRunningId('')
    }
  }

  return (
    <div className="page chaos-page">
      <div className="page-header">
        <div>
          <h1>Chaos Scenarios</h1>
          <p className="page-subtitle">
            Trigger the incident simulations on the AWS K3s node through SSM.
          </p>
        </div>
      </div>

      <AlertBanner tone="error">{error}</AlertBanner>
      <AlertBanner tone="success">{success}</AlertBanner>

      <form className="chaos-toolbar" onSubmit={loadScenarios}>
        <label className="field chaos-token-field">
          Chaos admin token
          <input
            type="password"
            value={token}
            onChange={(event) => setToken(event.target.value)}
            placeholder="Required"
          />
        </label>
        <label className="field chaos-namespace-field">
          Namespace
          <input value={namespace} onChange={(event) => setNamespace(event.target.value)} />
        </label>
        <label className="chaos-checkbox">
          <input
            type="checkbox"
            checked={autoRollback}
            onChange={(event) => setAutoRollback(event.target.checked)}
          />
          Auto-rollback bad deployment
        </label>
        <button type="submit" className="btn btn-primary" disabled={!token || loading}>
          {loading ? 'Connecting...' : 'Connect'}
        </button>
      </form>

      {loading && !data ? (
        <Spinner label="Checking Sentinel" />
      ) : (
        <>
          {data && (
            <section className="chaos-runner-status">
              <span className={data.configured ? 'status-pill status-approved' : 'status-pill status-rejected'}>
                {data.configured ? 'SSM configured' : 'SSM not configured'}
              </span>
              <span>{data.instance_id ?? 'No runner instance configured'}</span>
              <span>{data.workdir}</span>
            </section>
          )}

          <section className="chaos-grid">
            {scenarios.map((scenario) => (
              <article className="chaos-card" key={scenario.id}>
                <div>
                  <div className="chaos-card-heading">
                    <h2>{scenario.title}</h2>
                    {scenario.dangerous && <span className="status-pill status-rejected">Disruptive</span>}
                  </div>
                  <p className="chaos-family">{scenario.family}</p>
                  <p>{scenario.description}</p>
                </div>
                <button
                  type="button"
                  className={scenario.dangerous ? 'btn btn-danger' : 'btn btn-primary'}
                  disabled={!data?.configured || !token || Boolean(runningId)}
                  onClick={() => handleRun(scenario.id)}
                >
                  {runningId === scenario.id ? 'Starting...' : 'Run scenario'}
                </button>
              </article>
            ))}
          </section>
        </>
      )}

      {run && (
        <section className="chaos-output">
          <div className="chaos-output-header">
            <div>
              <h2>Latest Run</h2>
              <p>{run.command_id}</p>
            </div>
            <span className="status-pill">{run.status ?? run.status_details ?? 'Started'}</span>
          </div>
          {run.stdout && <pre>{run.stdout}</pre>}
          {run.stderr && <pre className="chaos-stderr">{run.stderr}</pre>}
        </section>
      )}
    </div>
  )
}
