import Icon from './Icon.jsx'

const ICON = { error: 'alertCircle', warn: 'alertTriangle', success: 'checkCircle', info: 'info' }

/**
 * tone: 'error' (default) | 'warn' | 'success' | 'info'
 * Errors are announced immediately (role="alert"); everything else politely.
 */
export default function AlertBanner({ children, tone = 'error', title, action, className = '' }) {
  if (!children && !title) return null
  return (
    <div className={`alert alert--${tone}${className ? ` ${className}` : ''}`} role={tone === 'error' ? 'alert' : 'status'}>
      <Icon name={ICON[tone] ?? 'info'} size={16} className="alert__icon" />
      <div className="alert__body">
        {title && <div className="alert__title">{title}</div>}
        {children}
      </div>
      {action && <div className="alert__action">{action}</div>}
    </div>
  )
}
