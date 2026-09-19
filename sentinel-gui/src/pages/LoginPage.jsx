import { useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { extractErrorMessage } from '../api/client.js'
import AlertBanner from '../components/ui/AlertBanner.jsx'
import Button from '../components/ui/Button.jsx'
import Field from '../components/ui/Field.jsx'
import Icon, { BrandMark } from '../components/ui/Icon.jsx'
import { useAuth } from '../context/AuthContext.jsx'
import { usePageTitle } from '../hooks/usePageTitle.js'

export default function LoginPage() {
  usePageTitle('Sign in')
  const { login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()

  const [form, setForm] = useState({ username: '', password: '' })
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const [showPassword, setShowPassword] = useState(false)

  function handleChange(event) {
    const { name, value } = event.target
    setForm((prev) => ({ ...prev, [name]: value }))
  }

  async function handleSubmit(event) {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await login(form.username, form.password)
      const redirectTo = location.state?.from?.pathname ?? '/'
      navigate(redirectTo, { replace: true })
    } catch (err) {
      setError(extractErrorMessage(err, 'Incorrect username or password.'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="auth-page">
      <main className="auth-card">
        <div className="auth-card__brand">
          <BrandMark size={36} />
          <div>
            <h1>Sentinel SRE Control Center</h1>
            <p className="auth-card__subtitle">Authorized SRE administrator access only.</p>
          </div>
        </div>

        <form onSubmit={handleSubmit} className="stack">
          <AlertBanner>{error}</AlertBanner>

          <Field label="Username">
            <input
              type="text"
              name="username"
              className="input"
              value={form.username}
              onChange={handleChange}
              required
              autoComplete="username"
              autoFocus
            />
          </Field>

          <Field label="Password">
            <PasswordInput
              value={form.password}
              onChange={handleChange}
              visible={showPassword}
              onToggle={() => setShowPassword((v) => !v)}
            />
          </Field>

          <Button type="submit" variant="primary" className="button--block" busy={submitting} busyLabel="Signing in…">
            Sign in
          </Button>
        </form>
      </main>
    </div>
  )
}

// Field injects id / aria-describedby into its direct child, so the input and
// its reveal toggle live inside one wrapper component that forwards them.
function PasswordInput({ visible, onToggle, ...inputProps }) {
  return (
    <div className="input-group input-group--trailing">
      <input
        type={visible ? 'text' : 'password'}
        name="password"
        className="input"
        required
        autoComplete="current-password"
        {...inputProps}
      />
      <span className="input-group__trailing">
        <button
          type="button"
          className="icon-button icon-button--sm"
          onClick={onToggle}
          aria-pressed={visible}
          aria-label={visible ? 'Hide password' : 'Show password'}
        >
          <Icon name={visible ? 'eyeOff' : 'eye'} size={15} />
        </button>
      </span>
    </div>
  )
}
