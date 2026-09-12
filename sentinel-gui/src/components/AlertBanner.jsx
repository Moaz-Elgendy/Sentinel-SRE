export default function AlertBanner({ children, tone = 'error' }) {
  if (!children) return null
  return <div className={`alert-banner alert-banner--${tone}`}>{children}</div>
}
