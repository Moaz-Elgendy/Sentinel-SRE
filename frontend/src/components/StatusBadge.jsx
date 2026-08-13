const STATUS_CLASSNAMES = {
  Pending: 'status-pill status-pending',
  'Under Review': 'status-pill status-review',
  Approved: 'status-pill status-approved',
  Rejected: 'status-pill status-rejected',
  Completed: 'status-pill status-completed',
}

export default function StatusBadge({ status }) {
  const className = STATUS_CLASSNAMES[status] ?? 'status-pill'
  return <span className={className}>{status}</span>
}
