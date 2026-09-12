import axios from 'axios'

// Points at sentinel-ai (see ../../sentinel-ai). Override with
// VITE_API_BASE_URL at build time for a non-local deployment.
const baseURL = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8080'

export const client = axios.create({ baseURL })

// Deliberately a different storage key from the citizen portal's
// `citizen_portal_token` — the two apps may run in browser profiles on the
// same machine during a demo, and a shared key would let one app read or
// clobber the other's session even though the tokens come from entirely
// separate auth systems (see sentinel-ai/app/core/security.py).
const TOKEN_KEY = 'sentinel_gui_token'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token) {
  if (token) {
    localStorage.setItem(TOKEN_KEY, token)
  } else {
    localStorage.removeItem(TOKEN_KEY)
  }
}

client.interceptors.request.use((config) => {
  const token = getToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// Centralizes "your session expired" handling so every page doesn't need
// its own 401 special-casing.
let onUnauthorized = null
export function registerUnauthorizedHandler(handler) {
  onUnauthorized = handler
}

client.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401 && onUnauthorized) {
      onUnauthorized()
    }
    return Promise.reject(error)
  }
)

// Normalizes FastAPI's error shape ({"detail": "..."} or a Pydantic
// validation array) into a single human-readable string for display. Also
// distinguishes a real HTTP error (has a response) from a request that
// never got a response at all — the latter is almost always the API being
// unreachable or a CORS rejection (sentinel-ai's SENTINEL_GUI_ORIGINS must
// include this app's origin — see sentinel-ai/app/core/config.py), not
// something wrong with what the admin submitted.
export function extractErrorMessage(error, fallback = 'Something went wrong. Please try again.') {
  if (error?.response) {
    const detail = error.response.data?.detail
    if (!detail) return fallback
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
      return detail.map((item) => item.msg ?? JSON.stringify(item)).join(', ')
    }
    return fallback
  }

  if (error?.request) {
    return `Could not reach Sentinel at ${client.defaults.baseURL}. It may be down, or this page's origin may not be in SENTINEL_GUI_ORIGINS (check the browser console for a CORS error).`
  }

  return fallback
}
