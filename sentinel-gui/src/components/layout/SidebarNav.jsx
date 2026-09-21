import { NavLink, useLocation } from 'react-router-dom'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useIncidentFeed } from '@/context/IncidentFeedContext'
import { cn } from '@/lib/utils'
import { DEMO_ITEM, NAV_GROUPS, isNavActive } from './nav.js'

function CountPill({ tone, count, label }) {
  if (!count) return null
  return (
    <span
      aria-label={`${count} ${label}`}
      title={`${count} ${label}`}
      className={cn(
        'tnum inline-flex h-4.5 min-w-4.5 items-center justify-center rounded px-1 text-[11px] leading-none font-medium',
        tone === 'warn' ? 'bg-warn-tint text-warn ring-1 ring-warn-edge' : 'bg-info-tint text-info ring-1 ring-info-edge'
      )}
    >
      {count}
    </span>
  )
}

function NavItem({ item, collapsed, onNavigate, counts }) {
  const { pathname } = useLocation()
  const active = isNavActive(item, pathname)
  const Icon = item.icon
  const link = (
    <NavLink
      to={item.to}
      end={item.end}
      onClick={onNavigate}
      aria-current={active ? 'page' : undefined}
      className={cn(
        'group relative flex h-8 items-center gap-2.5 rounded-md px-2.5 text-sm text-sidebar-foreground/75 transition-colors outline-none',
        'hover:bg-sidebar-accent hover:text-sidebar-accent-foreground focus-visible:ring-2 focus-visible:ring-sidebar-ring',
        active && 'bg-sidebar-accent font-medium text-sidebar-accent-foreground',
        collapsed && 'justify-center px-0'
      )}
    >
      {active && <span aria-hidden="true" className="absolute top-1.5 bottom-1.5 -left-2 w-0.5 rounded-full bg-sidebar-foreground" />}
      <Icon aria-hidden="true" className="size-4 shrink-0" />
      {!collapsed && <span className="flex-1 truncate">{item.label}</span>}
      {!collapsed && item.badge === 'incidents' && (
        <span className="flex items-center gap-1">
          <CountPill tone="warn" count={counts.awaiting} label="need attention" />
          <CountPill tone="info" count={counts.active} label="in progress" />
        </span>
      )}
      {collapsed && item.badge === 'incidents' && (counts.awaiting > 0 || counts.active > 0) && (
        <span aria-hidden="true" className={cn('absolute top-1 right-1.5 size-1.5 rounded-full', counts.awaiting > 0 ? 'bg-warn-solid' : 'bg-info-solid')} />
      )}
    </NavLink>
  )
  if (!collapsed) return link
  return (
    <Tooltip>
      <TooltipTrigger asChild>{link}</TooltipTrigger>
      <TooltipContent side="right">{item.label}</TooltipContent>
    </Tooltip>
  )
}

export function SidebarNav({ collapsed = false, onNavigate }) {
  const { active, awaiting } = useIncidentFeed()
  const counts = { active: active.length, awaiting: awaiting.length }
  return (
    <nav aria-label="Primary" className="flex flex-1 flex-col gap-5 overflow-y-auto px-3 py-3">
      {NAV_GROUPS.map((group) => (
        <div key={group.label} className="flex flex-col gap-0.5">
          {!collapsed && <p className="px-2.5 pb-1 text-xs font-medium text-sidebar-foreground/55">{group.label}</p>}
          {collapsed && <div aria-hidden="true" className="mx-2 mb-1 border-t border-sidebar-border first:hidden" />}
          {group.items.map((item) => (
            <NavItem key={item.to} item={item} collapsed={collapsed} onNavigate={onNavigate} counts={counts} />
          ))}
        </div>
      ))}
      <div className="mt-auto flex flex-col gap-0.5">
        {!collapsed && <p className="px-2.5 pb-1 text-xs font-medium text-sidebar-foreground/55">Demo utilities</p>}
        <NavItem item={DEMO_ITEM} collapsed={collapsed} onNavigate={onNavigate} counts={counts} />
      </div>
    </nav>
  )
}
