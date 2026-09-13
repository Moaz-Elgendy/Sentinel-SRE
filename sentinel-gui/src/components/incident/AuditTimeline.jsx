import { formatTimestamp, titleCase } from '../../utils/format.js'

/**
 * Renders `Incident.timeline` (see app/models/incident.py: TimelineEvent)
 * verbatim, oldest first. This is the same list LiveFlowDiagram derives
 * stage state from — nothing here is a separate, invented log.
 */
export default function AuditTimeline({ timeline }) {
  if (!timeline || timeline.length === 0) {
    return <p className="muted">No timeline events recorded yet.</p>
  }

  return (
    <ol className="audit-timeline">
      {timeline.map((event, index) => (
        // eslint-disable-next-line react/no-array-index-key
        <li key={index} className="audit-timeline__row">
          <span className="audit-timeline__time mono">{formatTimestamp(event.at)}</span>
          <span className="audit-timeline__phase">{titleCase(event.phase)}</span>
          <span className="audit-timeline__message">{event.message}</span>
        </li>
      ))}
    </ol>
  )
}
