import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import * as authApi from '../api/auth'
import { getToken, registerUnauthorizedHandler, setToken } from '../api/client'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [citizen, setCitizen] = useState(null)
  // 'loading' while we check for an existing session, then 'ready'.
  const [status, setStatus] = useState('loading')

  const loadCurrentCitizen = useCallback(async () => {
    if (!getToken()) {
      setCitizen(null)
      setStatus('ready')
      return
    }
    try {
      const current = await authApi.getCurrentCitizen()
      setCitizen(current)
    } catch {
      setToken(null)
      setCitizen(null)
    } finally {
      setStatus('ready')
    }
  }, [])

  useEffect(() => {
    loadCurrentCitizen()
  }, [loadCurrentCitizen])

  useEffect(() => {
    // If any API call comes back 401 (expired/invalid token), drop the
    // local session so the UI reflects reality instead of looking "stuck".
    registerUnauthorizedHandler(() => {
      setToken(null)
      setCitizen(null)
    })
  }, [])

  const login = useCallback(async (credentials) => {
    const { access_token } = await authApi.login(credentials)
    setToken(access_token)
    const current = await authApi.getCurrentCitizen()
    setCitizen(current)
    return current
  }, [])

  const register = useCallback(async (payload) => {
    await authApi.register(payload)
    // Registration doesn't return a token — log in right after so the
    // person isn't asked to type their password twice in a row.
    return login({ email: payload.email, password: payload.password })
  }, [login])

  const logout = useCallback(() => {
    setToken(null)
    setCitizen(null)
  }, [])

  const value = useMemo(
    () => ({
      citizen,
      isAuthenticated: Boolean(citizen),
      isLoading: status === 'loading',
      login,
      register,
      logout,
      refreshCitizen: loadCurrentCitizen,
      setCitizen,
    }),
    [citizen, status, login, register, logout, loadCurrentCitizen]
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
