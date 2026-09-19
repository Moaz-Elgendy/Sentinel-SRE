import { useEffect, useRef } from 'react'
import { Link, NavLink } from 'react-router-dom'
import { useSummary } from '../../context/SummaryContext.jsx'
import Icon, { BrandMark } from '../ui/Icon.jsx'

const NAV_ITEMS = [
  { to: '/', label: 'Dashboard', icon: 'dashboard', end: true },
  { to: '/incidents', label: 'Incidents', icon: 'alertTriangle', badge: true },
  { to: '/environment', label: 'Environment', icon: 'server' },
  { to: '/actions', label: 'Action History', icon: 'history' },
  { to: '/performance', label: 'Performance', icon: 'activity' },
]

const ADMIN_NAV_ITEMS = [
  { to: '/policies', label: 'Policies', icon: 'shield' },
  { to: '/rca-config', label: 'RCA & Diagnosis', icon: 'search' },
  { to: '/remediation-config', label: 'Remediation', icon: 'wrench' },
  { to: '/ai-config', label: 'AI & Reasoning', icon: 'cpu' },
  { to: '/monitoring-config', label: 'Monitoring', icon: 'eye' },
  { to: '/config-history', label: 'Configuration History', icon: 'fileText' },
]

function NavItem({ item, activeIncidents }) {
  const showBadge = item.badge && activeIncidents > 0
  return (
    <NavLink to={item.to} end={item.end} className="sidebar__link">
      <Icon name={item.icon} size={16} />
      {item.label}
      {showBadge && (
        <span className="sidebar__badge">
          {activeIncidents}
          <span className="sr-only"> active</span>
        </span>
      )}
    </NavLink>
  )
}

export default function Sidebar({ open, onClose }) {
  const { data: summary } = useSummary()
  const activeIncidents = summary?.incidents?.active_incidents ?? 0
  const ref = useRef(null)

  // Small screens: the sidebar is a modal drawer, so keep keyboard focus inside
  // it while open and let Escape close it.
  useEffect(() => {
    if (!open) return undefined
    const root = ref.current
    const focusable = () => [...root.querySelectorAll('a[href], button:not([disabled])')]
    focusable()[0]?.focus()

    function handleKeyDown(event) {
      if (event.key === 'Escape') {
        onClose()
        return
      }
      if (event.key !== 'Tab') return
      const items = focusable()
      const first = items[0]
      const last = items[items.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [open, onClose])

  return (
    <aside ref={ref} id="app-sidebar" className={`sidebar${open ? ' sidebar--open' : ''}`} aria-label="Sentinel navigation">
      <div className="sidebar__head">
        <Link to="/" className="sidebar__brand" aria-label="Sentinel SRE Control Center — dashboard">
          <BrandMark />
          <div>
            <div className="sidebar__brand-title">Sentinel</div>
            <div className="sidebar__brand-subtitle">SRE Control Center</div>
          </div>
        </Link>
        <button type="button" className="icon-button sidebar__close" onClick={onClose} aria-label="Close navigation">
          <Icon name="x" size={18} />
        </button>
      </div>

      <nav className="sidebar__group" aria-label="Operations">
        {NAV_ITEMS.map((item) => (
          <NavItem key={item.to} item={item} activeIncidents={activeIncidents} />
        ))}
      </nav>

      <div className="sidebar__label" id="admin-nav-label">
        Administration
      </div>
      <nav className="sidebar__group" aria-labelledby="admin-nav-label">
        {ADMIN_NAV_ITEMS.map((item) => (
          <NavItem key={item.to} item={item} activeIncidents={0} />
        ))}
      </nav>

      {/* Kept visually and structurally separate from the operational console
          above — this is an admin/demo utility for intentionally triggering a
          scenario during a presentation, not a Sentinel capability. */}
      <div className="sidebar__demo">
        <div className="sidebar__label" id="demo-nav-label">
          Demo utilities
        </div>
        <nav className="sidebar__group" aria-labelledby="demo-nav-label">
          <NavLink to="/demo" className="sidebar__link sidebar__link--demo">
            <Icon name="flask" size={16} />
            Chaos scenarios
          </NavLink>
        </nav>
      </div>
    </aside>
  )
}
