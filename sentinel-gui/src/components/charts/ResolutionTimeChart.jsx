import { Timer } from 'lucide-react'
import { useMemo } from 'react'
import { Bar, BarChart, CartesianGrid, ReferenceLine, XAxis, YAxis } from 'recharts'
import { listIncidents } from '@/api/incidents'
import { ChartContainer, ChartTooltip, ChartTooltipContent } from '@/components/ui/chart'
import { usePolling } from '@/hooks/usePolling'
import { formatSpan } from '@/utils/format'
import { rootCauseLabel } from '@/utils/incident'
import { EmptyState, ErrorState, SkeletonRows } from '../sentinel/States.jsx'

const config = { minutes: { label: 'Time to resolve', color: 'var(--chart-3)' } }
const fetchWindow = () => listIncidents({ limit: 200 })

/** Time from alert to recovery for the most recent resolved incidents, against the all-time average. */
export default function ResolutionTimeChart({ average, count = 20, height = 220 }) {
  const { data, error, loading, refetch } = usePolling(fetchWindow, { intervalMs: 60000 })

  const rows = useMemo(
    () =>
      (data?.incidents ?? [])
        .filter((i) => i.status === 'resolved' && i.resolved_at)
        .sort((a, b) => a.created_at - b.created_at)
        .slice(-count)
        .map((i) => ({
          id: i.id,
          alert: i.alertname,
          cause: i.hypothesis?.root_cause,
          day: new Date(i.created_at * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' }),
          seconds: Math.round(i.resolved_at - i.created_at),
          minutes: (i.resolved_at - i.created_at) / 60,
        })),
    [data, count]
  )

  if (loading) return <SkeletonRows rows={3} />
  if (error && !data) return <ErrorState title="Could not load resolution times" onRetry={refetch} />
  if (rows.length === 0) return <EmptyState compact icon={Timer} title="No resolved incidents yet" description="Recovery times are charted here once Sentinel has resolved some incidents." />

  return (
    <ChartContainer config={config} className="w-full" style={{ height }}>
      <BarChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: -8 }} barCategoryGap={3}>
        <CartesianGrid vertical={false} strokeDasharray="3 3" />
        <XAxis dataKey="day" tickLine={false} axisLine={false} tickMargin={8} interval="preserveStartEnd" minTickGap={28} />
        <YAxis tickLine={false} axisLine={false} width={48} tickFormatter={(v) => `${Math.round(v * 10) / 10}m`} />
        <ChartTooltip
          cursor={{ fill: 'var(--muted)', opacity: 0.5 }}
          content={
            <ChartTooltipContent
              hideIndicator
              labelFormatter={(_, payload) => {
                const p = payload?.[0]?.payload
                return p ? `${p.id}: ${p.alert}${p.cause ? `, ${rootCauseLabel(p.cause)}` : ''}` : ''
              }}
              formatter={(_, __, item) => <span className="tnum font-medium">{formatSpan(item.payload.seconds)}</span>}
            />
          }
        />
        {average != null && <ReferenceLine y={average / 60} stroke="var(--muted-foreground)" strokeDasharray="4 3" label={{ value: `avg ${formatSpan(average)}`, position: 'insideTopRight', fill: 'var(--muted-foreground)', fontSize: 11 }} />}
        <Bar dataKey="minutes" fill="var(--color-minutes)" radius={[2, 2, 0, 0]} />
      </BarChart>
    </ChartContainer>
  )
}
