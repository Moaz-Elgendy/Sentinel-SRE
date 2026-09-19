import { titleCase } from '../../utils/format.js'

const TONE = { critical: 'bad', warning: 'warn', info: 'info' }

export default function SeverityBadge({ severity }) {
  if (!severity) return <span className="faint">—</span>
  return (
    <span className="severity">
      <span className={`status-dot status-dot--${TONE[severity] ?? 'neutral'}`} aria-hidden="true" />
      {titleCase(severity)}
    </span>
  )
}
