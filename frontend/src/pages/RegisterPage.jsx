import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import AlertBanner from '../components/AlertBanner'
import { extractErrorMessage } from '../api/client'
import { useAuth } from '../context/AuthContext'

const EMPTY_FORM = {
  full_name: '',
  national_id: '',
  email: '',
  phone: '',
  password: '',
}

export default function RegisterPage() {
  const { register } = useAuth()
  const navigate = useNavigate()

  const [form, setForm] = useState(EMPTY_FORM)
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)

  function handleChange(event) {
    const { name, value } = event.target
    setForm((prev) => ({ ...prev, [name]: value }))
  }

  async function handleSubmit(event) {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      // Send phone as undefined rather than "" — the backend treats an
      // absent field differently from an explicit empty string.
      await register({ ...form, phone: form.phone || undefined })
      navigate('/requests', { replace: true })
    } catch (err) {
      setError(extractErrorMessage(err, 'Could not create your account.'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="auth-page">
      <form className="auth-card" onSubmit={handleSubmit}>
        <h1>Create an account</h1>
        <p className="auth-subtitle">Register to submit and track government service requests.</p>

        <AlertBanner>{error}</AlertBanner>

        <label className="field">
          <span>Full name</span>
          <input
            type="text"
            name="full_name"
            value={form.full_name}
            onChange={handleChange}
            required
            minLength={2}
            maxLength={120}
            autoComplete="name"
          />
        </label>

        <label className="field">
          <span>National ID</span>
          <input
            type="text"
            name="national_id"
            value={form.national_id}
            onChange={handleChange}
            required
            minLength={5}
            maxLength={30}
          />
        </label>

        <label className="field">
          <span>Email</span>
          <input
            type="email"
            name="email"
            value={form.email}
            onChange={handleChange}
            required
            autoComplete="email"
          />
        </label>

        <label className="field">
          <span>Phone (optional)</span>
          <input type="tel" name="phone" value={form.phone} onChange={handleChange} autoComplete="tel" />
        </label>

        <label className="field">
          <span>Password</span>
          <input
            type="password"
            name="password"
            value={form.password}
            onChange={handleChange}
            required
            minLength={8}
            maxLength={128}
            autoComplete="new-password"
          />
          <span className="field-hint">At least 8 characters.</span>
        </label>

        <button type="submit" className="btn btn-primary btn-block" disabled={submitting}>
          {submitting ? 'Creating account…' : 'Register'}
        </button>

        <p className="auth-switch">
          Already have an account? <Link to="/login">Log in</Link>
        </p>
      </form>
    </div>
  )
}
