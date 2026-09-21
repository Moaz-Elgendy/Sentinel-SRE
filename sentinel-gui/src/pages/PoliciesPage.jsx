import { applyPolicyChange, getPolicyConfig, previewPolicyChange } from '@/api/config'
import { ConfigPage, ReadOnlyPanel, SettingsSection } from '@/components/config/ConfigShell'
import { ListSetting, NumberSetting } from '@/components/config/SettingFields'
import { Badge } from '@/components/ui/badge'
import { useConfigEditor } from '@/hooks/useConfigEditor'
import { usePageTitle } from '@/hooks/usePageTitle'
import { listToText, textToList } from '@/utils/list'

const CONFIDENCE_FIELDS = [
  { field: 'confidence_restart', label: 'Restart a deployment' },
  { field: 'confidence_rollback', label: 'Roll back a deployment' },
  { field: 'confidence_scale', label: 'Scale a deployment' },
  { field: 'confidence_chaos_reset', label: 'Reset a chaos fault' },
]
const LIMIT_FIELDS = [
  { field: 'min_replicas', label: 'Minimum replicas', hint: 'Sentinel never scales below this.' },
  { field: 'max_replicas', label: 'Maximum replicas', hint: 'Sentinel never scales above this.' },
  { field: 'max_actions_per_incident', label: 'Actions per incident', hint: 'After this many, Sentinel escalates instead of trying again.' },
  { field: 'action_cooldown_seconds', label: 'Cooldown between actions (seconds)', hint: 'Minimum gap before the same workload is acted on again.' },
  { field: 'deployment_correlation_window_minutes', label: 'Deploy correlation window (minutes)', hint: 'How recent a deployment must be to count as a suspect.' },
]
const LIST_FIELDS = [
  { field: 'allowed_namespaces', label: 'Allowed namespaces', hint: 'Comma-separated. Sentinel acts only inside these.' },
  { field: 'allowed_deployments', label: 'Allowed deployments', hint: 'Comma-separated. Sentinel acts only on these. Can never include anything on the protected list.' },
]

const makeDraft = (current) => ({
  ...Object.fromEntries([...CONFIDENCE_FIELDS, ...LIMIT_FIELDS].map(({ field }) => [field, current[field]])),
  ...Object.fromEntries(LIST_FIELDS.map(({ field }) => [field, listToText(current[field])])),
})

function diffChanges(current, draft) {
  const changes = {}
  for (const { field } of [...CONFIDENCE_FIELDS, ...LIMIT_FIELDS]) {
    const value = Number(draft[field])
    if (draft[field] !== '' && !Number.isNaN(value) && value !== current[field]) changes[field] = value
  }
  for (const { field } of LIST_FIELDS) {
    // Allow-lists are sets: order is not a change.
    const draftList = [...textToList(draft[field] ?? '')].sort()
    const currentList = [...(current[field] ?? [])].sort()
    const same = draftList.length === currentList.length && draftList.every((v, i) => v === currentList[i])
    if (!same) changes[field] = textToList(draft[field] ?? '')
  }
  return changes
}

export default function PoliciesPage() {
  usePageTitle('Policies')
  const editor = useConfigEditor({ load: getPolicyConfig, previewChange: previewPolicyChange, applyChange: applyPolicyChange, makeDraft, diffChanges, loadErrorMessage: 'Could not load policy configuration.' })
  return (
    <ConfigPage editor={editor} description="Backed live by Sentinel’s Policy Engine. A change takes effect on the very next incident evaluation.">
      {editor.data && (
        <>
          <SettingsSection title="Confidence thresholds" description="How sure Sentinel must be of its diagnosis before it acts without asking. Higher is more cautious.">
            {CONFIDENCE_FIELDS.map((f) => (
              <NumberSetting key={f.field} editor={editor} step="0.01" {...f} />
            ))}
          </SettingsSection>
          <SettingsSection title="Limits and cooldowns" description="Hard limits on how much, and how often, Sentinel may act.">
            {LIMIT_FIELDS.map((f) => (
              <NumberSetting key={f.field} editor={editor} {...f} />
            ))}
          </SettingsSection>
          <SettingsSection title="Allow-lists" description="The only places Sentinel is permitted to act.">
            {LIST_FIELDS.map((f) => (
              <ListSetting key={f.field} editor={editor} {...f} />
            ))}
          </SettingsSection>
          <ReadOnlyPanel title="Protected: never editable, here or anywhere else" description="Sentinel will refuse to touch these even if an allow-list or a human authorization says otherwise.">
            <dl className="space-y-3 text-sm">
              {[
                ['Denied deployments', editor.data.protected.denied_deployments],
                ['Denied namespaces', editor.data.protected.denied_namespaces],
              ].map(([label, items]) => (
                <div key={label} className="flex flex-wrap items-baseline gap-x-6 gap-y-1.5">
                  <dt className="w-40 shrink-0 text-xs text-muted-foreground">{label}</dt>
                  <dd className="flex flex-wrap gap-1.5">
                    {items.length ? items.map((i) => <Badge key={i} variant="secondary" className="font-mono font-normal">{i}</Badge>) : '—'}
                  </dd>
                </div>
              ))}
            </dl>
          </ReadOnlyPanel>
        </>
      )}
    </ConfigPage>
  )
}
