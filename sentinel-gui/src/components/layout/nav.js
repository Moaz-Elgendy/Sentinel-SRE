import { Gauge, LayoutDashboard, ListChecks, ScrollText, Server, ShieldCheck, Siren, FlaskConical } from 'lucide-react'

// Configuration is one area with six tabs (see ConfigLayout); the old per-page
// routes are kept so existing bookmarks and links keep working.
export const CONFIG_TABS = [
  { to: '/policies', label: 'Policies', description: 'What Sentinel is allowed to do' },
  { to: '/rca-config', label: 'Diagnosis', description: 'Thresholds used to diagnose incidents' },
  { to: '/remediation-config', label: 'Remediation', description: 'Dry-run mode and the action ladder' },
  { to: '/ai-config', label: 'AI reasoning', description: 'LLM provider and behaviour' },
  { to: '/monitoring-config', label: 'Monitoring', description: 'What Sentinel watches, and how often' },
  { to: '/config-history', label: 'Change history', description: 'Every configuration change, restorable' },
]

export const NAV_GROUPS = [
  {
    label: 'Operate',
    items: [
      { to: '/', label: 'Command center', icon: LayoutDashboard, end: true },
      { to: '/incidents', label: 'Incidents', icon: Siren, badge: 'incidents' },
      { to: '/actions', label: 'Action ledger', icon: ListChecks },
      { to: '/logs', label: 'Sentinel logs', icon: ScrollText },
    ],
  },
  {
    label: 'Understand',
    items: [
      { to: '/environment', label: 'Environment', icon: Server },
      { to: '/performance', label: 'Performance', icon: Gauge },
    ],
  },
  {
    label: 'Govern',
    items: [{ to: '/policies', label: 'Guardrails & config', icon: ShieldCheck, matches: CONFIG_TABS.map((t) => t.to) }],
  },
]

export const DEMO_ITEM = { to: '/demo', label: 'Chaos scenarios', icon: FlaskConical }

export function isNavActive(item, pathname) {
  if (item.matches) return item.matches.some((p) => pathname === p || pathname.startsWith(`${p}/`))
  if (item.end) return pathname === item.to
  return pathname === item.to || pathname.startsWith(`${item.to}/`)
}

/** Breadcrumb trail for the topbar. */
export function resolveCrumbs(pathname) {
  if (pathname === '/') return [{ label: 'Command center' }]
  if (pathname.startsWith('/incidents/')) {
    return [{ label: 'Incidents', to: '/incidents' }, { label: decodeURIComponent(pathname.split('/')[2] ?? ''), mono: true }]
  }
  const tab = CONFIG_TABS.find((t) => t.to === pathname)
  if (tab) return [{ label: 'Configuration', to: '/policies' }, { label: tab.label }]
  const item = [...NAV_GROUPS.flatMap((g) => g.items), DEMO_ITEM].find((i) => i.to === pathname)
  return [{ label: item?.label ?? 'Not found' }]
}
