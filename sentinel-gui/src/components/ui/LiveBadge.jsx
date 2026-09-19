/** Shown only while an incident is non-terminal — i.e. Sentinel is still working on it. */
export default function LiveBadge() {
  return (
    <span className="live-badge">
      <span className="live-badge__dot" aria-hidden="true" />
      Live
    </span>
  )
}
