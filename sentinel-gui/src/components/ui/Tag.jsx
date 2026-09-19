/** Small inline label. tone: 'neutral' (default) | 'ok' | 'warn' | 'bad' | 'info' */
export default function Tag({ tone = 'neutral', children, title, className = '' }) {
  const classes = ['tag', tone !== 'neutral' && `tag--${tone}`, className].filter(Boolean).join(' ')
  return (
    <span className={classes} title={title}>
      {children}
    </span>
  )
}
