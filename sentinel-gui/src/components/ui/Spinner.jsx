/** Inline spinner + text, for small in-card waits (page-level waits use skeletons). */
export default function Spinner({ label = 'Loading…' }) {
  return (
    <div className="spinner-inline" role="status">
      <span className="spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  )
}
