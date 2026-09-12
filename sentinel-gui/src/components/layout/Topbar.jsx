import { useNavigate } from 'react-router-dom'
import { getDashboardSummary } from '../../api/dashboard.js'
import { useAuth } from '../../context/AuthContext.jsx'
import { usePolling } from '../../hooks/usePolling.js'
import StatusPill from '../StatusPill.jsx'

const STATUS_LABEL = {
  operational: 'All Systems Operational',
  degraded: 'Degraded',
  unknown: 'Status Unknown',
}

export default function Topbar() {
  const { admin, logout } = useAuth()
  const navigate = useNavigate()
  const { data: summary } = usePolling(getDashboardSummary, { intervalMs: 15000 })
  const overallStatus = summary?.system_health?.status ?? 'unknown'

  function handleLogout() {
    logout()
    navigate('/login', { replace: true })
  }

  return (
    <header className="topbar">
      <StatusPill status={overallStatus} label={STATUS_LABEL[overallStatus] ?? 'Status Unknown'} />

      <div className="topbar__spacer" />

      <div className="topbar__admin">
        <span className="topbar__admin-name">{admin?.username}</span>
        <button type="button" className="button button--ghost" onClick={handleLogout}>
          Sign out
        </button>
      </div>
    </header>
  )
}
