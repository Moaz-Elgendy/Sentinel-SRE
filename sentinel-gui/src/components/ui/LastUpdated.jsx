import { useNow } from '../../hooks/useNow.js'
import { formatRelativeTime } from '../../utils/format.js'
import Icon from './Icon.jsx'

/**
 * Tells the truth about data freshness. "Updated 8s ago" is measured from the
 * last successful fetch (it is not an animation); if the latest refresh failed
 * it says so and shows how old the data on screen actually is.
 */
export default function LastUpdated({ updatedAt, stale = false, busy = false }) {
  useNow(5000) // re-render so the age label keeps advancing between polls
  if (!updatedAt) return null
  const age = formatRelativeTime(updatedAt / 1000)
  return (
    <span className={`last-updated${stale ? ' last-updated--stale' : ''}`} title={new Date(updatedAt).toLocaleString()}>
      <Icon name={stale ? 'alertTriangle' : 'refresh'} size={12} className={busy ? 'is-spinning' : ''} />
      {stale ? `Couldn't refresh · showing data from ${age}` : `Updated ${age}`}
    </span>
  )
}
