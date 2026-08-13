import { NavLink, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

export default function Navbar() {
  const { isAuthenticated, citizen, logout } = useAuth()
  const navigate = useNavigate()

  function handleLogout() {
    logout()
    navigate('/login')
  }

  return (
    <header className="navbar">
      <div className="navbar-inner">
        <NavLink to="/" className="navbar-brand">
          <span className="navbar-brand-mark">DC</span>
          Digital Citizen Services
        </NavLink>

        <nav className="navbar-links">
          <NavLink to="/services" className="navbar-link">
            Services
          </NavLink>
          {isAuthenticated && (
            <NavLink to="/requests" className="navbar-link">
              My Requests
            </NavLink>
          )}
          {isAuthenticated && (
            <NavLink to="/profile" className="navbar-link">
              Profile
            </NavLink>
          )}
        </nav>

        <div className="navbar-actions">
          {isAuthenticated ? (
            <>
              <span className="navbar-user">{citizen?.full_name}</span>
              <button type="button" className="btn btn-ghost" onClick={handleLogout}>
                Log out
              </button>
            </>
          ) : (
            <>
              <NavLink to="/login" className="btn btn-ghost">
                Log in
              </NavLink>
              <NavLink to="/register" className="btn btn-primary">
                Register
              </NavLink>
            </>
          )}
        </div>
      </div>
    </header>
  )
}
