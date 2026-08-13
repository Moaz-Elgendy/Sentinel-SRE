import { useEffect, useState } from 'react'
import AlertBanner from '../components/AlertBanner'
import Spinner from '../components/Spinner'
import { extractErrorMessage } from '../api/client'
import * as profileApi from '../api/profile'
import { useAuth } from '../context/AuthContext'

export default function ProfilePage() {
  const { setCitizen } = useAuth()

  const [profile, setProfile] = useState(null)
  const [form, setForm] = useState({ full_name: '', phone: '' })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let cancelled = false
    profileApi
      .getProfile()
      .then((data) => {
        if (cancelled) return
        setProfile(data)
        setForm({ full_name: data.full_name, phone: data.phone ?? '' })
      })
      .catch((err) => {
        if (!cancelled) setError(extractErrorMessage(err, 'Could not load your profile.'))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  function handleChange(event) {
    const { name, value } = event.target
    setForm((prev) => ({ ...prev, [name]: value }))
  }

  async function handleSubmit(event) {
    event.preventDefault()
    setError(null)
    setNotice(null)
    setSaving(true)
    try {
      const updated = await profileApi.updateProfile({
        full_name: form.full_name,
        phone: form.phone || undefined,
      })
      setProfile(updated)
      setCitizen(updated)
      setNotice('Profile updated.')
    } catch (err) {
      setError(extractErrorMessage(err, 'Could not update your profile.'))
    } finally {
      setSaving(false)
    }
  }

  if (loading) return <Spinner label="Loading profile…" />

  return (
    <div className="page page-narrow">
      <div className="page-header">
        <h1>My Profile</h1>
      </div>

      <AlertBanner>{error}</AlertBanner>
      <AlertBanner tone="success">{notice}</AlertBanner>

      <form className="card" onSubmit={handleSubmit}>
        <label className="field">
          <span>Full name</span>
          <input type="text" name="full_name" value={form.full_name} onChange={handleChange} required minLength={2} />
        </label>

        <label className="field">
          <span>Phone</span>
          <input type="tel" name="phone" value={form.phone} onChange={handleChange} />
        </label>

        <label className="field">
          <span>Email</span>
          <input type="email" value={profile?.email ?? ''} disabled />
          <span className="field-hint">Email cannot be changed here.</span>
        </label>

        <label className="field">
          <span>National ID</span>
          <input type="text" value={profile?.national_id ?? ''} disabled />
        </label>

        <button type="submit" className="btn btn-primary" disabled={saving}>
          {saving ? 'Saving…' : 'Save changes'}
        </button>
      </form>
    </div>
  )
}
