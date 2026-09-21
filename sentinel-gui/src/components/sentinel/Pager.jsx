import { ChevronLeft, ChevronRight } from 'lucide-react'
import { Button } from '@/components/ui/button'

/** Client-side pager: "26–50 of 83" with previous / next. */
export function Pager({ page, pageSize, total, onPageChange, noun = 'items' }) {
  if (total <= pageSize) {
    return total > 0 ? <p className="text-xs text-muted-foreground">{total} {noun}</p> : null
  }
  const start = page * pageSize + 1
  const end = Math.min(total, (page + 1) * pageSize)
  const last = Math.ceil(total / pageSize) - 1
  return (
    <div className="flex items-center gap-3">
      <p className="tnum text-xs text-muted-foreground">
        {start}–{end} of {total} {noun}
      </p>
      <div className="flex items-center gap-1">
        <Button variant="outline" size="icon-sm" onClick={() => onPageChange(page - 1)} disabled={page === 0} aria-label="Previous page">
          <ChevronLeft />
        </Button>
        <Button variant="outline" size="icon-sm" onClick={() => onPageChange(page + 1)} disabled={page >= last} aria-label="Next page">
          <ChevronRight />
        </Button>
      </div>
    </div>
  )
}
