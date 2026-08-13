export default function AlertBanner({ tone = 'error', children }) {
  if (!children) return null
  return (
    <div className={`alert alert-${tone}`} role="alert">
      {children}
    </div>
  )
}
