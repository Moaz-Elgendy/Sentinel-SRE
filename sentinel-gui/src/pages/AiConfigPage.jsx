import { applyAiChange, getAiConfig, previewAiChange } from '@/api/config'
import { ConfigPage, ReadOnlyPanel, SettingsSection } from '@/components/config/ConfigShell'
import { NumberSetting, SelectSetting, TextSetting } from '@/components/config/SettingFields'
import { Badge } from '@/components/ui/badge'
import { useConfigEditor } from '@/hooks/useConfigEditor'
import { usePageTitle } from '@/hooks/usePageTitle'

const FIELDS = [
  { field: 'llm_provider', type: 'enum' },
  { field: 'openai_model', type: 'string' },
  { field: 'openai_timeout_seconds', type: 'float' },
  { field: 'openai_base_url', type: 'string' },
  { field: 'gemini_model', type: 'string' },
  { field: 'gemini_timeout_seconds', type: 'float' },
  { field: 'groq_model', type: 'string' },
  { field: 'groq_timeout_seconds', type: 'float' },
]

const makeDraft = (current) => Object.fromEntries(FIELDS.map(({ field }) => [field, current[field] ?? '']))
function diffChanges(current, draft) {
  const changes = {}
  for (const { field, type } of FIELDS) {
    if (!(field in current)) continue
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

const PROVIDER_LABEL = { openai: 'OpenAI', gemini: 'Gemini', groq: 'Groq' }

const PROVIDERS = [
  { key: 'openai', name: 'OpenAI', fields: ['openai_model', 'openai_timeout_seconds', 'openai_base_url'] },
  { key: 'gemini', name: 'Gemini', fields: ['gemini_model', 'gemini_timeout_seconds'] },
  { key: 'groq', name: 'Groq', fields: ['groq_model', 'groq_timeout_seconds'] },
]

const LABELS = {
  openai_model: 'Model',
  openai_timeout_seconds: 'Timeout (seconds)',
  openai_base_url: 'OpenAI-compatible base URL',
  gemini_model: 'Model',
  gemini_timeout_seconds: 'Timeout (seconds)',
  groq_model: 'Model',
  groq_timeout_seconds: 'Timeout (seconds)',
}

function KeyBadge({ configured }) {
  return configured ? <Badge variant="ok">Key configured</Badge> : <Badge variant="warn">No key: rules-only if this is the active provider</Badge>
}

export default function AiConfigPage() {
  usePageTitle('AI reasoning configuration')
  const editor = useConfigEditor({ load: getAiConfig, previewChange: previewAiChange, applyChange: applyAiChange, makeDraft, diffChanges, loadErrorMessage: 'Could not load AI configuration.' })
  const readOnly = editor.data?.read_only
  const active = editor.draft?.llm_provider

  return (
    <ConfigPage editor={editor} description="Provider, model and timeout for LLM-assisted diagnosis. The LLM can only narrow a hypothesis the rule engine already produced; it can never invent one.">
      {editor.data && (
        <>
          <SettingsSection title="Provider" description="Which LLM Sentinel consults. If it is unreachable, Sentinel carries on with rules only.">
            <SelectSetting editor={editor} field="llm_provider" label="Active provider" format={(v) => PROVIDER_LABEL[v] ?? v} />
          </SettingsSection>
          {PROVIDERS.filter((p) => p.fields.every((f) => f in editor.data.current)).map((provider) => (
            <SettingsSection key={provider.key} title={<span className="flex items-center gap-2">{provider.name}{active === provider.key && <Badge variant="info">Active</Badge>}</span>}>
              {provider.fields.map((field) =>
                field.endsWith('timeout_seconds') ? (
                  <NumberSetting key={field} editor={editor} field={field} label={LABELS[field]} />
                ) : (
                  <TextSetting key={field} editor={editor} field={field} label={LABELS[field]} hint={field === 'openai_base_url' ? 'Leave blank to use api.openai.com.' : undefined} />
                )
              )}
            </SettingsSection>
          ))}
          <ReadOnlyPanel title="API keys and fixed values" description={readOnly.description}>
            <dl className="divide-y text-sm">
              {PROVIDERS.filter((p) => `${p.key}_api_key_configured` in readOnly).map((p) => (
                <div key={p.key} className="flex items-center justify-between gap-4 py-2 first:pt-0">
                  <dt className="text-muted-foreground">{p.name} API key</dt>
                  <dd>
                    <KeyBadge configured={readOnly[`${p.key}_api_key_configured`]} />
                  </dd>
                </div>
              ))}
              <div className="flex items-baseline justify-between gap-4 py-2 last:pb-0">
                <dt className="text-muted-foreground">Temperature</dt>
                <dd className="text-right">
                  <span className="tnum font-mono">{readOnly.temperature}</span>
                  <span className="ml-2 text-xs text-muted-foreground">{readOnly.temperature_note}</span>
                </dd>
              </div>
            </dl>
          </ReadOnlyPanel>
        </>
      )}
    </ConfigPage>
  )
}
