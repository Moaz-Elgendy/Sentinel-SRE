import { Link } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

export default function HomePage() {
  const { isAuthenticated, citizen } = useAuth()

  return (
    <div className="page">
      <section className="hero">
        <h1>Digital Citizen Services Portal</h1>
        <p className="hero-subtitle">
          Renew documents, request certificates, and track government service requests online — one
          account, every service.
        </p>
        <div className="hero-actions">
          <Link to="/services" className="btn btn-primary">
            Browse services
          </Link>
          {isAuthenticated ? (
            <Link to="/requests" className="btn btn-ghost">
              View my requests
            </Link>
          ) : (
            <Link to="/register" className="btn btn-ghost">
              Create an account
            </Link>
          )}
        </div>
        {isAuthenticated && <p className="hero-greeting">Welcome back, {citizen?.full_name}.</p>}
      </section>

      <section className="feature-grid">
        <div className="feature-card">
          <h2>Browse services</h2>
          <p>See every government service available, what documents you'll need, and how long it takes.</p>
        </div>
        <div className="feature-card">
          <h2>Submit requests online</h2>
          <p>Skip the counter — submit a request in a few clicks and get notified as it progresses.</p>
        </div>
        <div className="feature-card">
          <h2>Track everything</h2>
          <p>Follow every request from submission to completion, with caseworker notes along the way.</p>
        </div>
      </section>
    </div>
  )
}
