import { CornerDownLeft, LogOut, Moon, Sun } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
} from '@/components/ui/command'
import { useAuth } from '@/context/AuthContext'
import { useIncidentFeed } from '@/context/IncidentFeedContext'
import { useTheme } from '@/context/ThemeContext'
import { incidentStatus } from '@/utils/incident'
import { statusLabel } from '@/utils/status'
import { CONFIG_TABS, DEMO_ITEM, NAV_GROUPS } from './nav.js'
import { SeverityBadge } from '../sentinel/SeverityBadge.jsx'

/** ⌘K / Ctrl+K: jump to any page, any recent incident, or an incident by id. */
export function CommandPalette({ open, onOpenChange }) {
  const navigate = useNavigate()
  const { recent, awaiting } = useIncidentFeed()
  const { resolvedTheme, setTheme } = useTheme()
  const { logout } = useAuth()
  const [query, setQuery] = useState('')

  function handleOpenChange(next) {
    if (!next) setQuery('')
    onOpenChange(next)
  }

  const incidents = useMemo(() => {
    const seen = new Set()
    return [...awaiting, ...recent].filter((i) => (seen.has(i.id) ? false : seen.add(i.id)))
  }, [recent, awaiting])

  const pages = [...NAV_GROUPS.flatMap((g) => g.items), DEMO_ITEM]
  const idQuery = query.trim()
  const looksLikeId = /^INC-/i.test(idQuery) && !incidents.some((i) => i.id.toLowerCase() === idQuery.toLowerCase())

  function go(to) {
    handleOpenChange(false)
    navigate(to)
  }

  return (
    <CommandDialog open={open} onOpenChange={handleOpenChange} title="Command palette" description="Search pages, incidents and actions">
      <CommandInput value={query} onValueChange={setQuery} placeholder="Search pages and incidents, or paste an incident ID…" />
      <CommandList className="max-h-96">
        <CommandEmpty>No matches.</CommandEmpty>
        {looksLikeId && (
          <CommandGroup heading="Go to incident">
            <CommandItem value={`open ${idQuery}`} onSelect={() => go(`/incidents/${encodeURIComponent(idQuery)}`)}>
              <CornerDownLeft />
              Open <span className="font-mono text-xs">{idQuery}</span>
            </CommandItem>
          </CommandGroup>
        )}
        <CommandGroup heading="Pages">
          {pages.map((page) => (
            <CommandItem key={page.to} value={`page ${page.label}`} onSelect={() => go(page.to)}>
              <page.icon />
              {page.label}
            </CommandItem>
          ))}
          {CONFIG_TABS.map((tab) => (
            <CommandItem key={tab.to} value={`config ${tab.label} ${tab.description}`} onSelect={() => go(tab.to)}>
              <span className="size-4" />
              <span>
                Configuration <span className="text-muted-foreground">/ {tab.label}</span>
              </span>
            </CommandItem>
          ))}
        </CommandGroup>
        {incidents.length > 0 && (
          <>
            <CommandSeparator />
            <CommandGroup heading="Incidents">
              {incidents.slice(0, 30).map((incident) => (
                <CommandItem
                  key={incident.id}
                  value={`${incident.id} ${incident.alertname} ${incident.app} ${incidentStatus(incident)}`}
                  onSelect={() => go(`/incidents/${incident.id}`)}
                >
                  <SeverityBadge severity={incident.severity} iconOnly />
                  <span className="truncate">
                    {incident.alertname} <span className="text-muted-foreground">on {incident.app}</span>
                  </span>
                  <span className="ml-auto flex shrink-0 items-center gap-2 text-xs text-muted-foreground">
                    <span className="font-mono">{incident.id}</span>
                    <span>{statusLabel(incidentStatus(incident))}</span>
                  </span>
                </CommandItem>
              ))}
            </CommandGroup>
          </>
        )}
        <CommandSeparator />
        <CommandGroup heading="Actions">
          <CommandItem value="toggle theme dark light" onSelect={() => { setTheme(resolvedTheme === 'dark' ? 'light' : 'dark'); handleOpenChange(false) }}>
            {resolvedTheme === 'dark' ? <Sun /> : <Moon />}
            Switch to {resolvedTheme === 'dark' ? 'light' : 'dark'} theme
          </CommandItem>
          <CommandItem value="sign out log out" onSelect={() => { handleOpenChange(false); logout() }}>
            <LogOut /> Sign out
          </CommandItem>
        </CommandGroup>
      </CommandList>
    </CommandDialog>
  )
}
