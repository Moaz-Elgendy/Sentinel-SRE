import { useEffect, useState } from 'react'
import { Outlet } from 'react-router-dom'
import { Sheet, SheetContent, SheetDescription, SheetTitle } from '@/components/ui/sheet'
import { Toaster } from '@/components/ui/sonner'
import { TooltipProvider } from '@/components/ui/tooltip'
import { ActivityProvider } from '@/context/ActivityContext'
import { IncidentFeedProvider } from '@/context/IncidentFeedContext'
import { SummaryProvider } from '@/context/SummaryContext'
import { CommandPalette } from './CommandPalette.jsx'
import { Sidebar, SidebarBrand, UserMenu } from './Sidebar.jsx'
import { SidebarNav } from './SidebarNav.jsx'
import { Topbar } from './Topbar.jsx'

const COLLAPSE_KEY = 'sentinel-sidebar-collapsed'

export default function AppLayout() {
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem(COLLAPSE_KEY) === '1'
    } catch {
      return false
    }
  })
  const [navOpen, setNavOpen] = useState(false)
  const [paletteOpen, setPaletteOpen] = useState(false)

  useEffect(() => {
    try {
      localStorage.setItem(COLLAPSE_KEY, collapsed ? '1' : '0')
    } catch {
      // Not persisted in private mode; the toggle still works for the session.
    }
  }, [collapsed])

  useEffect(() => {
    function onKeyDown(event) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setPaletteOpen((open) => !open)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  return (
    <TooltipProvider delayDuration={200}>
      <SummaryProvider>
        <IncidentFeedProvider>
          <ActivityProvider>
            <a
              href="#main"
              className="sr-only z-50 rounded-md bg-primary px-3 py-2 text-sm text-primary-foreground focus:not-sr-only focus:fixed focus:top-3 focus:left-3"
            >
              Skip to content
            </a>
            <div className="flex min-h-svh">
              <Sidebar collapsed={collapsed} onToggle={() => setCollapsed((c) => !c)} />
              <div className="flex min-w-0 flex-1 flex-col">
                <Topbar onOpenNav={() => setNavOpen(true)} onOpenPalette={() => setPaletteOpen(true)} />
                <main id="main" tabIndex={-1} className="mx-auto w-full max-w-400 flex-1 px-3 py-5 outline-none lg:px-6 lg:py-6">
                  <Outlet />
                </main>
              </div>
            </div>

            <Sheet open={navOpen} onOpenChange={setNavOpen}>
              <SheetContent side="left" className="w-64 gap-0 bg-sidebar p-0 text-sidebar-foreground" showCloseButton={false}>
                <SheetTitle className="sr-only">Navigation</SheetTitle>
                <SheetDescription className="sr-only">Primary navigation</SheetDescription>
                <SidebarBrand collapsed={false} />
                <SidebarNav onNavigate={() => setNavOpen(false)} />
                <div className="border-t border-sidebar-border p-2">
                  <UserMenu collapsed={false} />
                </div>
              </SheetContent>
            </Sheet>

            <CommandPalette open={paletteOpen} onOpenChange={setPaletteOpen} />
            <Toaster position="bottom-right" />
          </ActivityProvider>
        </IncidentFeedProvider>
      </SummaryProvider>
    </TooltipProvider>
  )
}
