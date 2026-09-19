import Icon from './Icon.jsx'
import Button from './Button.jsx'

/** Says what is empty and — where possible — what to do about it. */
export default function EmptyState({ icon = 'inbox', title, description, action, compact = false }) {
  return (
    <div className={`empty${compact ? ' empty--compact' : ''}`}>
      <div className="empty__icon">
        <Icon name={icon} size={20} />
      </div>
      <div className="empty__title">{title}</div>
      {description && <p className="empty__description">{description}</p>}
      {action && <div className="empty__actions">{action}</div>}
    </div>
  )
}

/** A full-panel load failure: what failed, why (if known), and a retry. */
export function ErrorState({ title = "Couldn't load this page", message, onRetry }) {
  return (
    <div className="card" role="alert">
      <div className="empty empty--error">
        <div className="empty__icon">
          <Icon name="alertCircle" size={20} />
        </div>
        <div className="empty__title">{title}</div>
        {message && <p className="empty__description">{message}</p>}
        {onRetry && (
          <div className="empty__actions">
            <Button icon="refresh" onClick={onRetry}>
              Try again
            </Button>
          </div>
        )}
      </div>
    </div>
  )
}
