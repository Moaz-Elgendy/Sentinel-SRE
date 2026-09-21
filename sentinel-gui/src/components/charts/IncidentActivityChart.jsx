import { useMemo } from 'react'
import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from 'recharts'
import { listIncidents } from '@/api/incidents'
import { ChartContainer, ChartLegend, ChartLegendContent, ChartTooltip, ChartTooltipContent } from '@/components/ui/chart'
import { usePolling } from '@/hooks/usePolling'
import { EmptyState, ErrorState, SkeletonRows } from '../sentinel/States.jsx'
import { BarChart3 } from 'lucide-react'

const WINDOW = 200
const config = {
  resolved: { label: 'Resolved', color: 'var(--chart-1)' },
  escalated: { label: 'Needs a human', color: 'var(--chart-2)' },
  active: { label: 'In progress', color: 'var(--chart-3)' },
}

const fetchWindow = () => listIncidents({ limit: WINDOW })
const dayKey = (date) => date.toLocaleDateString('en-CA')

/** Incidents per day by outcome, from the real incident list (latest 200). */
export default function IncidentActivityChart({ days = 14, height = 200 }) {
  const { data, error, loading, refetch } = usePolling(fetchWindow, { intervalMs: 60000 })

  const { rows, total, truncated } = useMemo(() => {
    const incidents = data?.incidents ?? []
    const buckets = new Map()
    const today = new Date()
    today.setHours(0, 0, 0, 0)
    for (let i = days - 1; i >= 0; i -= 1) {
      const d = new Date(today)
      d.setDate(today.getDate() - i)
      buckets.set(dayKey(d), {
        day: d.toLocaleDateString([], { month: 'short', day: 'numeric' }),
        resolved: 0,
        escalated: 0,
        active: 0,
      })
    }
    let counted = 0
    for (const incident of incidents) {
      const bucket = buckets.get(dayKey(new Date(incident.created_at * 1000)))
      if (!bucket) continue
      counted += 1
      if (incident.status === 'escalated') bucket.escalated += 1
      else if (incident.status === 'resolved' || incident.status === 'auto_resolved') bucket.resolved += 1
      else bucket.active += 1
    }
    return { rows: [...buckets.values()], total: counted, truncated: incidents.length >= WINDOW }
  }, [data, days])

  if (loading) return <SkeletonRows rows={3} />
  if (error && !data) return <ErrorState title="Could not load incident history" onRetry={refetch} />
  if (total === 0) {
    return <EmptyState compact icon={BarChart3} title={`No incidents in the last ${days} days`} description="When Sentinel handles incidents they are charted here by outcome." />
  }

  return (
    <div>
      <ChartContainer config={config} className="w-full" style={{ height }}>
        <BarChart data={rows} margin={{ top: 4, right: 4, bottom: 0, left: -20 }} barCategoryGap={4}>
          <CartesianGrid vertical={false} strokeDasharray="3 3" />
          <XAxis dataKey="day" tickLine={false} axisLine={false} tickMargin={8} interval="preserveStartEnd" minTickGap={24} />
          <YAxis allowDecimals={false} tickLine={false} axisLine={false} width={40} />
          <ChartTooltip cursor={{ fill: 'var(--muted)', opacity: 0.5 }} content={<ChartTooltipContent indicator="dot" />} />
          <ChartLegend content={<ChartLegendContent />} />
          <Bar dataKey="resolved" stackId="a" fill="var(--color-resolved)" />
          <Bar dataKey="escalated" stackId="a" fill="var(--color-escalated)" />
          <Bar dataKey="active" stackId="a" fill="var(--color-active)" radius={[2, 2, 0, 0]} />
        </BarChart>
      </ChartContainer>
      {truncated && <p className="mt-1 text-[11px] text-muted-foreground">Based on the latest {WINDOW} incidents; older days may be under-counted.</p>}
    </div>
  )
}
