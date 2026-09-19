import Button from './Button.jsx'
import Icon from './Icon.jsx'

export default function Pagination({ offset, pageSize, total, onChange, previousLabel = 'Previous', nextLabel = 'Next' }) {
  if (!total) return null
  const from = offset + 1
  const to = Math.min(offset + pageSize, total)
  return (
    <nav className="pagination" aria-label="Pagination">
      <span className="pagination__count">
        {from}–{to} of {total}
      </span>
      <div className="pagination__buttons">
        <Button size="sm" icon="arrowLeft" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - pageSize))}>
          {previousLabel}
        </Button>
        <Button size="sm" disabled={offset + pageSize >= total} onClick={() => onChange(offset + pageSize)}>
          {nextLabel}
          <Icon name="arrowRight" size={14} />
        </Button>
      </div>
    </nav>
  )
}
