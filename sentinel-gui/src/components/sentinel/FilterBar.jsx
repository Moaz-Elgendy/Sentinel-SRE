import { Search, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { cn } from '@/lib/utils'

export function SearchInput({ value, onChange, placeholder = 'Search…', className, label = 'Search' }) {
  return (
    <div className={cn('relative', className)}>
      <Search aria-hidden="true" className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
      <Input type="search" value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} aria-label={label} className="h-8 pr-7 pl-8" />
      {value && (
        <Button variant="ghost" size="icon-xs" className="absolute top-1/2 right-1 -translate-y-1/2 text-muted-foreground" onClick={() => onChange('')} aria-label="Clear search">
          <X />
        </Button>
      )}
    </div>
  )
}

/** A compact labelled select. `options`: [{ value, label }]; the first is treated as "no filter". */
export function FilterSelect({ label, value, onChange, options, className, width = 'w-40' }) {
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger size="sm" aria-label={label} className={cn('h-8', width, className)}>
        <span className="mr-1 text-muted-foreground">{label}:</span>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {options.map((option) => (
          <SelectItem key={option.value} value={option.value}>
            {option.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
