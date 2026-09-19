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
    <ol className="audit">
      {timeline.map((event, index) => (
        // eslint-disable-next-line react/no-array-index-key
        <li key={index} className="audit__row">
          <time className="audit__time mono" dateTime={new Date(event.at * 1000).toISOString()}>
            {formatTimestamp(event.at)}
          </time>
          <span className="audit__phase">{titleCase(event.phase)}</span>
          <span className="audit__message">{event.message}</span>
        </li>
      ))}
    </ol>
  )
}
