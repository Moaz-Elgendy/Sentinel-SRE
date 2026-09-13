import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import * as authApi from '../api/auth.js'
import { getToken, registerUnauthorizedHandler, setToken } from '../api/client.js'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [admin, setAdmin] = useState(null)
  // 'loading' while we check for an existing session, then 'ready'.
  const [status, setStatus] = useState('loading')

  const loadCurrentAdmin = useCallback(async () => {
    if (!getToken()) {
      setAdmin(null)
      setStatus('ready')
      return
    }
    try {
      const current = await authApi.me()
      setAdmin(current)
    } catch {
      setToken(null)
      setAdmin(null)
    } finally {
      setStatus('ready')
    }
  }, [])

  useEffect(() => {
    loadCurrentAdmin()
  }, [loadCurrentAdmin])

  useEffect(() => {
    // If any API call comes back 401 (expired/invalid token), drop the
    // local session so the UI reflects reality instead of looking "stuck".
    registerUnauthorizedHandler(() => {
      setToken(null)
      setAdmin(null)
    })
  }, [])

  const login = useCallback(async (username, password) => {
    const { access_token: accessToken } = await authApi.login(username, password)
    setToken(accessToken)
    const current = await authApi.me()
    setAdmin(current)
    return current
  }, [])

  const logout = useCallback(() => {
    setToken(null)
    setAdmin(null)
  }, [])

  const value = useMemo(
    () => ({
      admin,
      isAuthenticated: Boolean(admin),
      isLoading: status === 'loading',
      login,
      logout,
    }),
    [admin, status, login, logout]
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) {
    throw new Error('useAuth must be used within an AuthProvider')
  }
  return ctx
}
