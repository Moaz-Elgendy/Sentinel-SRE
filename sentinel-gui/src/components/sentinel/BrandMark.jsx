import { cn } from '@/lib/utils'

/** Sentinel's mark: a shield around a single watchful point. Monochrome; colour is for state. */
export function BrandMark({ className }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" className={cn('size-6', className)}>
      <path d="M12 2.5 4.5 5.4v5.7c0 4.6 3.1 8.6 7.5 10.4 4.4-1.8 7.5-5.8 7.5-10.4V5.4L12 2.5Z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
      <circle cx="12" cy="11.5" r="2.6" fill="currentColor" />
    </svg>
  )
}
