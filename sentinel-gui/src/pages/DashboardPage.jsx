import { lazy, Suspense } from 'react'
import { BarChart3 } from 'lucide-react'
import { ActivityPanel } from '@/components/dashboard/ActivityPanel'
import { AuthorityPanel } from '@/components/dashboard/AuthorityPanel'
import { InFlightPanel } from '@/components/dashboard/InFlightPanel'
import { PostureBanner } from '@/components/dashboard/PostureBanner'
import { SentinelPanel } from '@/components/dashboard/SentinelPanel'
import { ServicesPanel } from '@/components/dashboard/ServicesPanel'
import { VitalsStrip } from '@/components/dashboard/VitalsStrip'
import { Panel } from '@/components/sentinel/Panel'
import { ErrorState, SkeletonRows } from '@/components/sentinel/States'
import { useActivity } from '@/context/ActivityContext'
import { useIncidentFeed } from '@/context/IncidentFeedContext'
import { useSummary } from '@/context/SummaryContext'
import { usePageTitle } from '@/hooks/usePageTitle'
import { usePerformance } from '@/hooks/usePerformance'

const IncidentActivityChart = lazy(() => import('@/components/charts/IncidentActivityChart'))

/**
 * Command center: the whole environment on one screen. Top to bottom it answers
 * "do I need to act?", "what are the numbers?", "what is happening?", and
 * "what has Sentinel been doing?" — in that order of urgency.
 */
export default function DashboardPage() {
  usePageTitle('Command center')
  const { data: summary, error, loading, refetch } = useSummary()
  const { recent, active, awaiting, loading: feedLoading } = useIncidentFeed()
  const { watchers } = useActivity()
  const { data: performance } = usePerformance()

  if (error && !summary) {
    return <ErrorState title="Could not load the command center" message="The Sentinel API did not respond." onRetry={refetch} className="py-24" />
  }

  const watchersOk = watchers ? watchers.items.every((w) => w.connected) : undefined

  return (
    <div className="space-y-4">
      <PostureBanner environment={summary?.environment} />
      <VitalsStrip summary={summary} performance={performance} active={active} awaiting={awaiting} />

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_24rem]">
        <div className="min-w-0 space-y-4">
          <InFlightPanel awaiting={awaiting} active={active} loading={feedLoading} watchersOk={watchersOk} />
          <ActivityPanel incidents={recent} />
          <Panel title="Incidents, last 14 days" icon={BarChart3} description="By outcome, from Sentinel’s incident history">
            <Suspense fallback={<SkeletonRows rows={3} />}>
              <IncidentActivityChart days={14} />
            </Suspense>
          </Panel>
        </div>
        <div className="min-w-0 space-y-4">
          <ServicesPanel services={summary?.system_health?.services ?? []} loading={loading} />
          <SentinelPanel summary={summary} />
          <AuthorityPanel />
        </div>
      </div>
    </div>
  )
}
