import { isLiveStatus, statusLabel, statusTone } from '../../utils/status.js'

/**
 * The single way to show a status. Colour is never the only signal: the label
 * is always rendered as text. The pulsing dot appears only for states that are
 * genuinely in progress (investigating / remediating / validating).
 */
export default function StatusPill({ status, label, size }) {
  const tone = statusTone(status)
  const live = isLiveStatus(status)
  const classes = [
    'status-pill',
    `status-pill--${tone}`,
    size === 'lg' && 'status-pill--lg',
    live && 'status-pill--live',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <span className={classes}>
      <span className="status-pill__dot" aria-hidden="true" />
      {label ?? statusLabel(status)}
    </span>
  )
}

/** A bare indicator dot with an accessible name — for dense lists. */
export function StatusDot({ status, label }) {
  return (
    <span className={`status-dot status-dot--${statusTone(status)}`} role="img" aria-label={label ?? statusLabel(status)} />
  )
}
