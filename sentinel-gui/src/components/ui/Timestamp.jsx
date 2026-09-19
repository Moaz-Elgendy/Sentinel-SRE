import { formatRelativeTime, formatTimestamp } from '../../utils/format.js'

/**
 * Relative by default ("4m ago") because that is what scanning needs, with the
 * exact local date/time available on hover and to assistive tech via <time>.
 */
export default function Timestamp({ epoch, absolute = false, className = '' }) {
  if (epoch == null) return <span className="faint">—</span>
  const exact = formatTimestamp(epoch)
  return (
    <time
      className={`num${className ? ` ${className}` : ''}`}
      dateTime={new Date(epoch * 1000).toISOString()}
      title={exact}
    >
      {absolute ? exact : formatRelativeTime(epoch)}
    </time>
  )
}
