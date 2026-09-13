export function formatTimestamp(epochSeconds) {
  if (epochSeconds == null) return '—'
  return new Date(epochSeconds * 1000).toLocaleString()
}

export function formatRelativeTime(epochSeconds) {
  if (epochSeconds == null) return '—'
  const diffSeconds = Date.now() / 1000 - epochSeconds
  if (diffSeconds < 0) return 'just now'
  if (diffSeconds < 60) return `${Math.floor(diffSeconds)}s ago`
  if (diffSeconds < 3600) return `${Math.floor(diffSeconds / 60)}m ago`
  if (diffSeconds < 86400) return `${Math.floor(diffSeconds / 3600)}h ago`
  return `${Math.floor(diffSeconds / 86400)}d ago`
}

export function formatDuration(seconds) {
  if (seconds == null) return '—'
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  const minutes = Math.floor(seconds / 60)
  const remSeconds = Math.round(seconds % 60)
  if (minutes < 60) return `${minutes}m ${remSeconds}s`
  const hours = Math.floor(minutes / 60)
  return `${hours}h ${minutes % 60}m`
}

export function formatPercent(fraction, fallback = 'No data yet') {
  if (fraction == null) return fallback
  return `${(fraction * 100).toFixed(1)}%`
}

export function titleCase(value) {
  if (!value) return '—'
  return String(value)
    .split('_')
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ')
}
