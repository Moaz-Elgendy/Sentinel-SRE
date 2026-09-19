import { createContext, useCallback, useContext, useMemo, useState } from 'react'
import Icon from './Icon.jsx'

const ToastContext = createContext(null)
const ICON = { success: 'checkCircle', error: 'alertCircle', info: 'info' }
let nextId = 1

/**
 * Brief confirmation of a meaningful action ("Change restored"). Toasts
 * announce politely, dismiss themselves and can be closed manually. They are
 * never used for errors that need a decision — those stay inline.
 */
export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([])

  const dismiss = useCallback((id) => {
    setToasts((current) => current.filter((toast) => toast.id !== id))
  }, [])

  const push = useCallback(
    (message, { tone = 'success', duration = 4500 } = {}) => {
      const id = nextId++
      setToasts((current) => [...current, { id, message, tone }])
      setTimeout(() => dismiss(id), duration)
    },
    [dismiss]
  )

  const value = useMemo(() => ({ push }), [push])

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toast-region" role="region" aria-label="Notifications">
        {toasts.map((toast) => (
          <div key={toast.id} className={`toast toast--${toast.tone}`} role="status">
            <Icon name={ICON[toast.tone] ?? 'info'} size={16} />
            <div className="toast__message">{toast.message}</div>
            <button type="button" className="icon-button icon-button--sm" aria-label="Dismiss" onClick={() => dismiss(toast.id)}>
              <Icon name="x" size={14} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast() {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast must be used within a ToastProvider')
  return ctx.push
}
