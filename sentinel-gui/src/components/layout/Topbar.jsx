import { useNavigate } from 'react-router-dom'
import { useAuth } from '../../context/AuthContext.jsx'
import { useSummary } from '../../context/SummaryContext.jsx'
import { HEALTH_HEADLINE } from '../../utils/status.js'
import Button from '../ui/Button.jsx'
import Icon from '../ui/Icon.jsx'
import StatusPill from '../ui/StatusPill.jsx'

export default function Topbar({ onMenuClick, menuOpen, menuButtonRef }) {
  const { admin, logout } = useAuth()
  const navigate = useNavigate()
  const { data: summary, error } = useSummary()

  // If the latest poll failed we cannot honestly claim any health state, so
  // say that instead of leaving a possibly-stale "operational" on screen.
  const overallStatus = error ? 'unknown' : (summary?.system_health?.status ?? 'unknown')
  const label = error ? 'Status unavailable' : (HEALTH_HEADLINE[overallStatus] ?? HEALTH_HEADLINE.unknown)

  function handleLogout() {
    logout()
    navigate('/login', { replace: true })
  }

  return (
    <header className="topbar">
      <button
        type="button"
        ref={menuButtonRef}
        className="icon-button topbar__menu"
        onClick={onMenuClick}
        aria-label="Open navigation"
        aria-expanded={menuOpen}
        aria-controls="app-sidebar"
      >
        <Icon name="menu" size={18} />
      </button>

      <StatusPill status={overallStatus} label={label} />

      <div className="topbar__spacer" />

      <div className="topbar__user">
        <span className="topbar__avatar" aria-hidden="true">
          {admin?.username?.charAt(0) ?? '?'}
        </span>
        <span className="topbar__name">{admin?.username}</span>
        <Button variant="ghost" size="sm" icon="logOut" onClick={handleLogout}>
          Sign out
        </Button>
      </div>
    </header>
  )
}
