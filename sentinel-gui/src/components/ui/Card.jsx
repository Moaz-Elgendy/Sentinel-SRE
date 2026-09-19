import { useId } from 'react'

/**
 * The one surface primitive. `flush` removes body padding (for tables that
 * run edge to edge). `tone="warn"` marks a card that needs human attention.
 */
export default function Card({
  title,
  description,
  actions,
  tone,
  flush = false,
  muted = false,
  className = '',
  children,
  ...rest
}) {
  const titleId = useId()
  const classes = [
    'card',
    flush && 'card--flush',
    tone && `card--${tone}`,
    muted && 'card--muted',
    className,
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <section className={classes} aria-labelledby={title ? titleId : undefined} {...rest}>
      {(title || actions) && (
        <div className="card__header">
          <div>
            {title && (
              <h2 className="card__title" id={titleId}>
                {title}
              </h2>
            )}
            {description && <p className="card__description">{description}</p>}
          </div>
          {actions && <div className="card__actions">{actions}</div>}
        </div>
      )}
      <div className="card__body">{children}</div>
    </section>
  )
}
