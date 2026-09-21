import { ChevronsUpDown, LogOut, PanelLeftClose, PanelLeftOpen } from 'lucide-react'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useAuth } from '@/context/AuthContext'
import { useSummary } from '@/context/SummaryContext'
import { cn } from '@/lib/utils'
import { BrandMark } from '../sentinel/BrandMark.jsx'
import { SidebarNav } from './SidebarNav.jsx'

export function SidebarBrand({ collapsed }) {
  const { data } = useSummary()
  const env = data?.environment
  return (
    <div className={cn('flex h-12 shrink-0 items-center gap-2.5 border-b border-sidebar-border px-4', collapsed && 'justify-center px-0')}>
      <BrandMark className="size-6 shrink-0 text-sidebar-foreground" />
      {!collapsed && (
        <div className="min-w-0 leading-tight">
          <p className="text-sm font-semibold tracking-tight">Sentinel</p>
          <p className="truncate text-[11px] text-sidebar-foreground/60" title={env ? `${env.name} · ${env.customer_id}` : undefined}>
            {env?.name ?? 'Autonomous SRE'}
          </p>
        </div>
      )}
    </div>
  )
}

export function UserMenu({ collapsed }) {
  const { admin, logout } = useAuth()
  const name = admin?.username ?? 'admin'
  const trigger = (
    <button
      type="button"
      className={cn(
        'flex h-10 w-full items-center gap-2.5 rounded-md px-2 text-left text-sm outline-none transition-colors hover:bg-sidebar-accent focus-visible:ring-2 focus-visible:ring-sidebar-ring',
        collapsed && 'justify-center px-0'
      )}
    >
      <Avatar className="size-6">
        <AvatarFallback className="bg-sidebar-accent text-[11px] font-medium uppercase">{name.slice(0, 1)}</AvatarFallback>
      </Avatar>
      {!collapsed && (
        <>
          <span className="flex-1 truncate">{name}</span>
          <ChevronsUpDown aria-hidden="true" className="size-3.5 text-sidebar-foreground/50" />
        </>
      )}
    </button>
  )
  return (
    <DropdownMenu>
      {collapsed ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <DropdownMenuTrigger asChild>{trigger}</DropdownMenuTrigger>
          </TooltipTrigger>
          <TooltipContent side="right">{name}</TooltipContent>
        </Tooltip>
      ) : (
        <DropdownMenuTrigger asChild>{trigger}</DropdownMenuTrigger>
      )}
      <DropdownMenuContent side="top" align="start" className="w-52">
        <DropdownMenuLabel className="font-normal">
          <p className="text-xs text-muted-foreground">Signed in as</p>
          <p className="truncate text-sm font-medium">{name}</p>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={logout}>
          <LogOut /> Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export function Sidebar({ collapsed, onToggle }) {
  return (
    <aside
      className={cn(
        'sticky top-0 hidden h-svh shrink-0 flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground transition-[width] duration-200 lg:flex',
        collapsed ? 'w-14' : 'w-58'
      )}
    >
      <SidebarBrand collapsed={collapsed} />
      <SidebarNav collapsed={collapsed} />
      <div className="flex flex-col gap-1 border-t border-sidebar-border p-2">
        <UserMenu collapsed={collapsed} />
        <Button
          variant="ghost"
          size={collapsed ? 'icon' : 'sm'}
          onClick={onToggle}
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          className={cn('text-sidebar-foreground/60 hover:text-sidebar-foreground', collapsed ? 'mx-auto' : 'justify-start')}
        >
          {collapsed ? <PanelLeftOpen /> : <PanelLeftClose />}
          {!collapsed && 'Collapse'}
        </Button>
      </div>
    </aside>
  )
}
