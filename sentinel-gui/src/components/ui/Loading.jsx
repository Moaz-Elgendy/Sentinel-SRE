// Loading states are skeletons shaped like the content that is coming, so the
// layout doesn't jump when data arrives. Each announces itself once to
// assistive tech; the shapes themselves are hidden from it.

export function Skeleton({ width = '100%', height = 14, className = '', style }) {
  return <span className={`skeleton ${className}`} style={{ width, height, ...style }} aria-hidden="true" />
}

function Region({ label, children }) {
  return (
    <div role="status" aria-live="polite" aria-busy="true">
      <span className="sr-only">{label}</span>
      {children}
    </div>
  )
}

function SkeletonCard({ lines = 3, height }) {
  return (
    <div className="skeleton-card" style={height ? { minHeight: height } : undefined}>
      <Skeleton width="38%" height={14} />
      {Array.from({ length: lines }, (_, i) => (
        <Skeleton key={i} width={`${92 - i * 14}%`} height={12} />
      ))}
    </div>
  )
}

/** Generic page: a title block and a few cards. */
export function PageSkeleton({ label = 'Loading…', cards = 2 }) {
  return (
    <Region label={label}>
      <div className="page">
        <div className="stack" style={{ gap: 8 }}>
          <Skeleton width={180} height={22} />
          <Skeleton width={280} height={12} />
        </div>
        {Array.from({ length: cards }, (_, i) => (
          <SkeletonCard key={i} lines={4} />
        ))}
      </div>
    </Region>
  )
}

/** A table card: toolbar + rows. */
export function TableSkeleton({ label = 'Loading…', rows = 8 }) {
  return (
    <Region label={label}>
      <div className="page">
        <div className="stack" style={{ gap: 8 }}>
          <Skeleton width={160} height={22} />
          <Skeleton width={260} height={12} />
        </div>
        <div className="card">
          <div className="toolbar">
            <Skeleton width={260} height={34} />
            <Skeleton width={140} height={34} />
          </div>
          <div className="card__body stack" style={{ gap: 18 }}>
            {Array.from({ length: rows }, (_, i) => (
              <Skeleton key={i} height={14} />
            ))}
          </div>
        </div>
      </div>
    </Region>
  )
}

export function DashboardSkeleton({ label = 'Loading Sentinel status…' }) {
  return (
    <Region label={label}>
      <div className="page">
        <div className="stack" style={{ gap: 8 }}>
          <Skeleton width={200} height={22} />
          <Skeleton width={240} height={12} />
        </div>
        <SkeletonCard lines={1} height={168} />
        <div className="grid grid--2">
          <SkeletonCard lines={5} />
          <SkeletonCard lines={5} />
        </div>
        <SkeletonCard lines={4} />
      </div>
    </Region>
  )
}

export function DetailSkeleton({ label = 'Loading…' }) {
  return (
    <Region label={label}>
      <div className="page">
        <div className="stack" style={{ gap: 8 }}>
          <Skeleton width={120} height={12} />
          <Skeleton width={320} height={24} />
          <Skeleton width={260} height={12} />
        </div>
        <SkeletonCard lines={1} height={84} />
        <div className="incident-layout">
          <SkeletonCard lines={6} height={320} />
          <div className="stack">
            <SkeletonCard lines={4} />
            <SkeletonCard lines={4} />
          </div>
        </div>
      </div>
    </Region>
  )
}
