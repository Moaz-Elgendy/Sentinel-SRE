import axios from 'axios'

// Points at Citizen Service (see ../../citizen-service). Override with
// VITE_API_BASE_URL at build time for a non-local deployment.
const baseURL = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

export const client = axios.create({ baseURL })

const TOKEN_KEY = 'citizen_portal_token'

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
// validation array) into a single human-readable string for display.
// Also distinguishes a real HTTP error (has a response) from a request
// that never got a response at all — the latter is almost always the API
// being unreachable or a CORS rejection, not something wrong with what
// the person submitted, so it gets a distinctly more useful message
// instead of silently reusing the generic fallback.
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
    // The request went out but no response came back at all — the API is
    // unreachable, down, or the browser blocked the response (commonly a
    // CORS rejection). Check the browser console/network tab for the
    // specific reason.
    return `Could not reach the server at ${client.defaults.baseURL}. It may be down, or this page's origin may not be allowed to call it (check the browser console for a CORS error).`
  }

  return fallback
}
