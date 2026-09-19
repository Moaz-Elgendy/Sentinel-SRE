import Icon from './Icon.jsx'

/**
 * variant: 'secondary' (default) | 'primary' | 'ghost' | 'caution' | 'danger'
 * `busy` disables the button, shows an inline spinner and (optionally) swaps
 * the label — so an in-flight action is always visibly acknowledged.
 */
export default function Button({
  variant = 'secondary',
  size,
  busy = false,
  busyLabel,
  icon,
  className = '',
  children,
  disabled,
  type = 'button',
  ...rest
}) {
  const classes = [
    'button',
    variant !== 'secondary' && `button--${variant}`,
    size === 'sm' && 'button--sm',
    className,
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <button type={type} className={classes} disabled={disabled || busy} aria-busy={busy || undefined} {...rest}>
      {busy ? <span className="spinner spinner--sm" aria-hidden="true" /> : icon && <Icon name={icon} size={14} />}
      {busy && busyLabel ? busyLabel : children}
    </button>
  )
}
