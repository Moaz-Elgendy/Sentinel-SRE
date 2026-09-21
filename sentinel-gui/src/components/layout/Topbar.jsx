import { Menu, Search } from 'lucide-react'
import { Link, useLocation } from 'react-router-dom'
import { Breadcrumb, BreadcrumbItem, BreadcrumbLink, BreadcrumbList, BreadcrumbPage, BreadcrumbSeparator } from '@/components/ui/breadcrumb'
import { Button } from '@/components/ui/button'
import { Kbd } from '@/components/ui/kbd'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useSummary } from '@/context/SummaryContext'
import { cn } from '@/lib/utils'
import { LiveIndicator } from '../sentinel/LiveIndicator.jsx'
import { ThemeToggle } from '../sentinel/ThemeToggle.jsx'
import { resolveCrumbs } from './nav.js'
import { PostureChip } from './PostureChip.jsx'

function ModeChip() {
  const { data } = useSummary()
  const mode = data?.sentinel?.mode
  if (!mode) return null
  const dry = mode === 'dry_run'
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Link
          to="/remediation-config"
          className={cn(
            'hidden h-6 items-center rounded-md border px-2 text-xs font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring md:inline-flex',
            dry ? 'border-warn-edge bg-warn-tint text-warn' : 'text-muted-foreground hover:text-foreground'
          )}
        >
          {dry ? 'Dry run' : 'Autonomous'}
        </Link>
      </TooltipTrigger>
      <TooltipContent className="max-w-64">
        {dry
          ? 'Dry-run mode: Sentinel decides and records actions but changes nothing in the cluster.'
          : 'Autonomous mode: Sentinel executes approved actions itself, within policy.'}
      </TooltipContent>
    </Tooltip>
  )
}

export function Topbar({ onOpenNav, onOpenPalette }) {
  const { pathname } = useLocation()
  const crumbs = resolveCrumbs(pathname)
  return (
    <header className="sticky top-0 z-30 flex h-12 shrink-0 items-center gap-3 border-b bg-background/85 px-3 backdrop-blur supports-backdrop-filter:bg-background/70 lg:px-6">
      <Button variant="ghost" size="icon" className="lg:hidden" onClick={onOpenNav} aria-label="Open navigation">
        <Menu />
      </Button>
      <Breadcrumb className="min-w-0 flex-1">
        <BreadcrumbList className="flex-nowrap text-sm">
          {crumbs.map((crumb, i) => {
            const last = i === crumbs.length - 1
            return (
              <span key={crumb.label} className="contents">
                <BreadcrumbItem className={cn('min-w-0', !last && 'hidden sm:inline-flex')}>
                  {last || !crumb.to ? (
                    <BreadcrumbPage className={cn('truncate', crumb.mono && 'font-mono text-xs')}>{crumb.label}</BreadcrumbPage>
                  ) : (
                    <BreadcrumbLink asChild>
                      <Link to={crumb.to}>{crumb.label}</Link>
                    </BreadcrumbLink>
                  )}
                </BreadcrumbItem>
                {!last && <BreadcrumbSeparator className="hidden sm:block" />}
              </span>
            )
          })}
        </BreadcrumbList>
      </Breadcrumb>
      <div className="flex shrink-0 items-center gap-2">
        <PostureChip />
        <ModeChip />
        <span aria-hidden="true" className="hidden h-4 w-px bg-border sm:block" />
        <LiveIndicator />
        <Button variant="outline" size="sm" onClick={onOpenPalette} className="hidden gap-2 text-muted-foreground sm:inline-flex">
          <Search />
          <span>Search</span>
          <Kbd>⌘K</Kbd>
        </Button>
        <Button variant="ghost" size="icon" onClick={onOpenPalette} className="sm:hidden" aria-label="Search">
          <Search />
        </Button>
        <ThemeToggle />
      </div>
    </header>
  )
}
