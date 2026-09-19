import { useCallback, useEffect, useRef, useState } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import { SummaryProvider } from '../../context/SummaryContext.jsx'
import Sidebar from './Sidebar.jsx'
import Topbar from './Topbar.jsx'

export default function AppLayout() {
  const menuButtonRef = useRef(null)
  const location = useLocation()

  // The drawer is remembered as open *for a specific path*, so navigating
  // anywhere closes it without needing an effect to reset state.
  const [openForPath, setOpenForPath] = useState(null)
  const navOpen = openForPath === location.pathname

  // If the viewport grows past the drawer breakpoint, drop drawer state.
  useEffect(() => {
    const query = window.matchMedia('(min-width: 961px)')
    const handleChange = () => {
      if (query.matches) setOpenForPath(null)
    }
    query.addEventListener('change', handleChange)
    return () => query.removeEventListener('change', handleChange)
  }, [])

  const closeNav = useCallback(() => {
    setOpenForPath(null)
    menuButtonRef.current?.focus()
  }, [])

  function handleSkip(event) {
    event.preventDefault()
    document.getElementById('main')?.focus()
  }

  return (
    <SummaryProvider>
      <a className="skip-link" href="#main" onClick={handleSkip}>
        Skip to content
      </a>
      <div className="app-shell">
        <Sidebar open={navOpen} onClose={closeNav} />
        {navOpen && <div className="scrim" onClick={closeNav} aria-hidden="true" />}
        <div className="app-shell__main">
          <Topbar onMenuClick={() => setOpenForPath(location.pathname)} menuOpen={navOpen} menuButtonRef={menuButtonRef} />
          <main id="main" tabIndex={-1} className="app-shell__content">
            <Outlet />
          </main>
        </div>
      </div>
    </SummaryProvider>
  )
}
