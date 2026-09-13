const TONE_BY_STATUS = {
  operational: 'ok',
  healthy: 'ok',
  true: 'ok',
  degraded: 'warn',
  escalated: 'warn',
  unknown: 'unknown',
  null: 'unknown',
  critical: 'bad',
  down: 'bad',
  false: 'bad',
}

export default function StatusPill({ status, label }) {
  const tone = TONE_BY_STATUS[String(status)] ?? 'unknown'
  return (
    <span className={`status-pill status-pill--${tone}`}>
      <span className="status-pill__dot" aria-hidden="true" />
      {label ?? String(status)}
    </span>
  )
}
