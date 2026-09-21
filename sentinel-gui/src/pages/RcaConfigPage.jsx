import { Link } from 'react-router-dom'
import { applyRcaChange, getRcaConfig, previewRcaChange } from '@/api/config'
import { ConfigPage, ReadOnlyPanel, SettingsSection } from '@/components/config/ConfigShell'
import { NumberSetting } from '@/components/config/SettingFields'
import { Badge } from '@/components/ui/badge'
import { useConfigEditor } from '@/hooks/useConfigEditor'
import { usePageTitle } from '@/hooks/usePageTitle'
import { formatBytes } from '@/utils/format'
import { rootCauseLabel } from '@/utils/incident'

const THRESHOLDS = [
  { field: 'max_error_rate', label: 'Max error rate', step: '0.01', hint: 'A fraction: 0.05 is 5%. Used to correlate evidence and to judge recovery.' },
  { field: 'max_p95_seconds', label: 'Max p95 latency (seconds)', step: '0.1' },
  { field: 'max_cpu_cores', label: 'Max CPU (cores)', step: '0.1' },
  { field: 'max_memory_bytes', label: 'Max memory (bytes)', step: '1000000' },
]
const TIMING = [
  { field: 'settle_seconds', label: 'Settle period before validating (seconds)', hint: 'How long to wait after an action before checking recovery.' },
  { field: 'timeout_seconds', label: 'Validation timeout (seconds)', hint: 'Give up on confirming recovery after this long.' },
  { field: 'poll_interval_seconds', label: 'Validation poll interval (seconds)' },
]
const FIELDS = [...THRESHOLDS, ...TIMING]

const makeDraft = (current) => Object.fromEntries(FIELDS.map(({ field }) => [field, current[field]]))
function diffChanges(current, draft) {
  const changes = {}
  for (const { field } of FIELDS) {
    const value = Number(draft[field])
    if (draft[field] !== '' && !Number.isNaN(value) && value !== current[field]) changes[field] = value
  }
  return changes
}

export default function RcaConfigPage() {
  usePageTitle('Diagnosis configuration')
  const editor = useConfigEditor({ load: getRcaConfig, previewChange: previewRcaChange, applyChange: applyRcaChange, makeDraft, diffChanges, loadErrorMessage: 'Could not load diagnosis configuration.' })
  const readOnly = editor.data?.read_only
  return (
    <ConfigPage editor={editor} description="Evidence and recovery-validation thresholds, live on Sentinel’s real correlation and validation steps. The incident page shows evidence against these same limits.">
      {editor.data && (
        <>
          <SettingsSection title="Evidence and validation thresholds" description="A reading past one of these is treated as unhealthy, both when diagnosing and when checking that a fix worked.">
            {THRESHOLDS.map((f) => (
              <NumberSetting
                key={f.field}
                editor={editor}
                {...f}
                extra={f.field === 'max_memory_bytes' && Number(editor.draft.max_memory_bytes) > 0 ? <p className="mt-1 text-xs text-muted-foreground">= {formatBytes(Number(editor.draft.max_memory_bytes))}</p> : null}
              />
            ))}
          </SettingsSection>
          <SettingsSection title="Validation timing" description="How Sentinel waits for, and polls, recovery after acting.">
            {TIMING.map((f) => (
              <NumberSetting key={f.field} editor={editor} {...f} />
            ))}
          </SettingsSection>
          <ReadOnlyPanel title="What controls Sentinel’s diagnosis" description={readOnly.rule_based_detection.description}>
            <dl className="divide-y text-sm">
              {[
                ['LLM confidence ceiling', readOnly.rule_based_detection.llm_confidence_ceiling],
                ['LLM confidence delta cap', readOnly.rule_based_detection.llm_confidence_delta_cap],
                ['Rule confidence max', readOnly.rule_based_detection.rule_confidence_max],
              ].map(([label, value]) => (
                <div key={label} className="flex justify-between gap-4 py-2 first:pt-0">
                  <dt className="text-muted-foreground">{label}</dt>
                  <dd className="tnum font-mono">{value}</dd>
                </div>
              ))}
              <div className="flex justify-between gap-4 py-2 last:pb-0">
                <dt className="text-muted-foreground">Deployment correlation window</dt>
                <dd>
                  {readOnly.deployment_correlation_window_minutes.value} min, edit in{' '}
                  <Link to="/policies" className="underline underline-offset-2">
                    Policies
                  </Link>
                </dd>
              </div>
            </dl>
            <h3 className="mt-4 mb-1.5 text-xs font-medium text-muted-foreground">Root cause taxonomy</h3>
            <ul className="flex flex-wrap gap-1.5">
              {readOnly.root_causes.map((rc) => (
                <li key={rc}>
                  <Badge variant="secondary" className="font-normal">
                    {rootCauseLabel(rc)}
                  </Badge>
                </li>
              ))}
            </ul>
          </ReadOnlyPanel>
        </>
      )}
    </ConfigPage>
  )
}
