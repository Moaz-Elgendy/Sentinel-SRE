import { LogViewer } from '@/components/logs/LogViewer'
import { PageHeader } from '@/components/sentinel/PageHeader'
import { usePageTitle } from '@/hooks/usePageTitle'

export default function SentinelLogsPage() {
  usePageTitle('Sentinel logs')
  return (
    <div className="space-y-4">
      <PageHeader
        title="Sentinel logs"
        description="Sentinel’s own backend logs: startup, evidence collection, diagnosis, policy, remediation, escalation and errors. These are not the monitored application’s logs."
      />
      <LogViewer height="h-[calc(100svh-17rem)] min-h-96" />
    </div>
  )
}
