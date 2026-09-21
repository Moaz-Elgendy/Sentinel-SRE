export function formatTimestamp(epochSeconds) {
  if (epochSeconds == null) return '—'
  return new Date(epochSeconds * 1000).toLocaleString()
}

// Clock time only (e.g. "14:03:22") — for dense timelines where the date is
// already obvious from context.
export function formatClock(epochSeconds) {
  if (epochSeconds == null) return '—'
  return new Date(epochSeconds * 1000).toLocaleTimeString([], {
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

export function formatRelativeTime(epochSeconds) {
  if (epochSeconds == null) return '—'
  const diffSeconds = Date.now() / 1000 - epochSeconds
  if (diffSeconds < 5) return 'just now'
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

/** Clock time with milliseconds, for log lines (e.g. "14:03:22.481"). */
export function formatClockMs(epochSeconds) {
  if (epochSeconds == null) return '—'
  const date = new Date(epochSeconds * 1000)
  const hms = date.toLocaleTimeString([], { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
  return `${hms}.${String(date.getMilliseconds()).padStart(3, '0')}`
}

/** "Sep 20, 12:01" — compact date+time for tables where the year is obvious. */
export function formatShortDateTime(epochSeconds) {
  if (epochSeconds == null) return '—'
  return new Date(epochSeconds * 1000).toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function formatBytes(bytes) {
  if (bytes == null) return '—'
  // Decimal (SI) units, matching how Sentinel's thresholds are configured
  // (700000000 bytes is "700 MB"), so a limit and a reading always compare cleanly.
  const abs = Math.abs(bytes)
  if (abs < 1000) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = abs / 1000
  let i = 0
  while (value >= 1000 && i < units.length - 1) {
    value /= 1000
    i += 1
  }
  return `${bytes < 0 ? '-' : ''}${value >= 100 ? value.toFixed(0) : value.toFixed(1)} ${units[i]}`
}

/** Whole-second duration without decimals for long spans: "3m 32s", "1h 04m". */
export function formatSpan(seconds) {
  if (seconds == null || Number.isNaN(seconds)) return '—'
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, '0')}s`
  const h = Math.floor(m / 60)
  if (h < 48) return `${h}h ${String(m % 60).padStart(2, '0')}m`
  return `${Math.floor(h / 24)}d ${h % 24}h`
}

/** "memory_leak" → "Memory leak" (sentence case reads better than Title Case in dense UI). */
export function sentenceCase(value) {
  if (!value) return '—'
  const text = String(value).replace(/_/g, ' ')
  return text.charAt(0).toUpperCase() + text.slice(1)
}
