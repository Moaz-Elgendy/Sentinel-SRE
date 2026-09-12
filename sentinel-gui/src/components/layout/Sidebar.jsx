import { NavLink } from 'react-router-dom'

const NAV_ITEMS = [
  { to: '/', label: 'Dashboard', end: true },
  { to: '/incidents', label: 'Incidents' },
  { to: '/environment', label: 'Environment' },
  { to: '/actions', label: 'Action History' },
  { to: '/performance', label: 'Performance' },
]

export default function Sidebar() {
  return (
    <aside className="sidebar">
      <div className="sidebar__brand">
        <span className="sidebar__brand-mark" aria-hidden="true" />
        <div>
          <div className="sidebar__brand-title">Sentinel</div>
          <div className="sidebar__brand-subtitle">SRE Control Center</div>
        </div>
      </div>

      <nav className="sidebar__nav">
        {NAV_ITEMS.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            className={({ isActive }) => `sidebar__link${isActive ? ' sidebar__link--active' : ''}`}
          >
            {item.label}
          </NavLink>
        ))}
      </nav>

      {/* Kept visually and structurally separate from the operational
          console above it — this is an admin/demo utility for
          intentionally triggering a scenario during a presentation, not a
          Sentinel capability, and it should never read as one. */}
      <div className="sidebar__demo">
        <div className="sidebar__demo-label">Demo utilities</div>
        <NavLink
          to="/demo"
          className={({ isActive }) => `sidebar__link sidebar__link--demo${isActive ? ' sidebar__link--active' : ''}`}
        >
          Chaos scenarios
        </NavLink>
      </div>
    </aside>
  )
}
