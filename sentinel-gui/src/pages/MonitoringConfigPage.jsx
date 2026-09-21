import { applyMonitoringChange, getMonitoringConfig, previewMonitoringChange } from '@/api/config'
import { ConfigPage, ReadOnlyPanel, SettingsSection } from '@/components/config/ConfigShell'
import { NumberSetting, TextSetting } from '@/components/config/SettingFields'
import { StatusBadge } from '@/components/sentinel/StatusBadge'
import { Badge } from '@/components/ui/badge'
import { useConfigEditor } from '@/hooks/useConfigEditor'
import { usePageTitle } from '@/hooks/usePageTitle'

const FIELDS = [
  { field: 'prometheus_url', label: 'Prometheus URL', type: 'string' },
  { field: 'prometheus_timeout_seconds', label: 'Prometheus timeout (seconds)', type: 'float' },
  { field: 'loki_url', label: 'Loki URL', type: 'string' },
  { field: 'loki_timeout_seconds', label: 'Loki timeout (seconds)', type: 'float' },
]

const makeDraft = (current) => Object.fromEntries(FIELDS.map(({ field }) => [field, current[field]]))
function diffChanges(current, draft) {
  const changes = {}
  for (const { field, type } of FIELDS) {
    const value = draft[field]
    if (type === 'float') {
      const num = Number(value)
      if (value !== '' && !Number.isNaN(num) && num !== current[field]) changes[field] = num
    } else if (value !== current[field]) {
      changes[field] = value
    }
  }
  return changes
}

export default function MonitoringConfigPage() {
  usePageTitle('Monitoring configuration')
  const editor = useConfigEditor({ load: getMonitoringConfig, previewChange: previewMonitoringChange, applyChange: applyMonitoringChange, makeDraft, diffChanges, loadErrorMessage: 'Could not load monitoring configuration.' })
  const readOnly = editor.data?.read_only
  const k8s = readOnly?.kubernetes

  return (
    <ConfigPage editor={editor} description="Where Sentinel reads metrics and logs from. The Environment page can test that each one is reachable.">
      {editor.data && (
        <>
          <SettingsSection title="Prometheus" description="Metrics: error rate, latency, CPU, memory.">
            <TextSetting editor={editor} field="prometheus_url" label="URL" />
            <NumberSetting editor={editor} field="prometheus_timeout_seconds" label="Timeout (seconds)" />
          </SettingsSection>
          <SettingsSection title="Loki" description="Logs: error lines and samples around an incident.">
            <TextSetting editor={editor} field="loki_url" label="URL" />
            <NumberSetting editor={editor} field="loki_timeout_seconds" label="Timeout (seconds)" />
          </SettingsSection>
          <ReadOnlyPanel title="Kubernetes connection" description={readOnly.description}>
            <dl className="divide-y text-sm">
              {[
                ['Mode', k8s.mode],
                ['Namespace', <span key="n" className="font-mono text-xs">{k8s.namespace}</span>],
                ['Reachable', <StatusBadge key="r" status={k8s.available ? 'healthy' : 'down'} label={k8s.available ? 'Reachable' : `Unreachable${k8s.init_error ? `: ${k8s.init_error}` : ''}`} />],
                ['Prometheus bearer token', <Badge key="p" variant={readOnly.prometheus_bearer_token_configured ? 'ok' : 'neutral'}>{readOnly.prometheus_bearer_token_configured ? 'Configured' : 'Not set'}</Badge>],
                ['Loki bearer token', <Badge key="l" variant={readOnly.loki_bearer_token_configured ? 'ok' : 'neutral'}>{readOnly.loki_bearer_token_configured ? 'Configured' : 'Not set'}</Badge>],
              ].map(([label, value]) => (
                <div key={label} className="flex items-center justify-between gap-4 py-2 first:pt-0 last:pb-0">
                  <dt className="text-muted-foreground">{label}</dt>
                  <dd className="text-right">{value}</dd>
                </div>
              ))}
            </dl>
          </ReadOnlyPanel>
          <ReadOnlyPanel title="Health checks" description={readOnly.health_checks.description}>
            <p className="text-sm text-muted-foreground">{readOnly.health_checks.note}</p>
          </ReadOnlyPanel>
        </>
      )}
    </ConfigPage>
  )
}
