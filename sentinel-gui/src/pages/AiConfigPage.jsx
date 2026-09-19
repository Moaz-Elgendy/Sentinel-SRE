import { applyAiChange, getAiConfig, previewAiChange } from '../api/config.js'
import { ChangeReview, ConfigActionBar, LastChanged, ReadOnlyCard } from '../components/config/ConfigParts.jsx'
import AlertBanner from '../components/ui/AlertBanner.jsx'
import Card from '../components/ui/Card.jsx'
import { ErrorState } from '../components/ui/EmptyState.jsx'
import Field from '../components/ui/Field.jsx'
import { PageSkeleton } from '../components/ui/Loading.jsx'
import PageHeader from '../components/ui/PageHeader.jsx'
import Tag from '../components/ui/Tag.jsx'
import { useConfigEditor } from '../hooks/useConfigEditor.js'
import { usePageTitle } from '../hooks/usePageTitle.js'
import { titleCase } from '../utils/format.js'

// `type` drives which input renders below. Timeout bounds come from the
// backend's /api/config/ai `bounds` payload (single source of truth — see
// ai_admin.py's TIMEOUT_BOUNDS), never hardcoded here.
const FIELDS = [
  { field: 'llm_provider', label: 'Provider', type: 'enum' },
  { field: 'openai_model', label: 'OpenAI model', type: 'string' },
  { field: 'openai_timeout_seconds', label: 'OpenAI timeout (seconds)', type: 'float' },
  { field: 'openai_base_url', label: 'OpenAI-compatible base URL', type: 'string', hint: 'Leave blank to use api.openai.com.' },
  { field: 'gemini_model', label: 'Gemini model', type: 'string' },
  { field: 'gemini_timeout_seconds', label: 'Gemini timeout (seconds)', type: 'float' },
]

function makeDraft(current) {
  return Object.fromEntries(FIELDS.map(({ field }) => [field, current[field]]))
}

function diffChanges(current, draft) {
  const changes = {}
  for (const { field, type } of FIELDS) {
    const draftValue = draft[field]
    if (type === 'float') {
      const num = Number(draftValue)
      if (!Number.isNaN(num) && num !== current[field]) changes[field] = num
    } else if (draftValue !== current[field]) {
      changes[field] = draftValue
    }
  }
  return changes
}

function KeyStatus({ configured }) {
  return configured ? <Tag tone="ok">Configured</Tag> : <Tag tone="warn">Not configured — rule-based only if this is the active provider</Tag>
}

export default function AiConfigPage() {
  usePageTitle('AI & Reasoning')
  const editor = useConfigEditor({
    load: getAiConfig,
    previewChange: previewAiChange,
    applyChange: applyAiChange,
    makeDraft,
    diffChanges,
    loadErrorMessage: 'Could not load AI/reasoning configuration.',
  })
  const { data, draft, changes, preview } = editor

  if (editor.loading) return <PageSkeleton label="Loading AI/reasoning configuration…" cards={2} />
  if (editor.loadError) {
    return <ErrorState title="Couldn't load AI configuration" message={editor.loadError} onRetry={editor.retryLoad} />
  }
  if (!data || !draft) return null

  const readOnly = data.read_only
  const bounds = data.bounds

  return (
    <div className="page">
      <PageHeader
        title="AI & Reasoning"
        subtitle={
          <>
            Provider, model, timeout, and base URL for LLM-assisted root cause analysis.
            <LastChanged at={data.last_changed_at} by={data.last_changed_by} />
          </>
        }
      />

      {editor.justApplied && !preview && (
        <AlertBanner tone="success">Configuration applied — Sentinel is using the new values now.</AlertBanner>
      )}
      {editor.actionError && <AlertBanner>{editor.actionError}</AlertBanner>}

      {preview ? (
        <ChangeReview
          preview={preview}
          reason={editor.reason}
          onReasonChange={editor.setReason}
          applying={editor.applying}
          onApply={editor.apply}
          onCancel={editor.cancelReview}
        />
      ) : (
        <>
          <Card title="Provider & model">
            <div className="form-grid">
              {FIELDS.map(({ field, label, type, hint }) => (
                <Field
                  key={field}
                  label={label}
                  hint={type === 'float' ? `Allowed range ${bounds[field].min}–${bounds[field].max}` : hint}
                  modified={field in changes}
                >
                  {type === 'enum' ? (
                    <select className="select" value={draft[field]} onChange={(e) => editor.setField(field, e.target.value)}>
                      {bounds[field].choices.map((choice) => (
                        <option key={choice} value={choice}>
                          {titleCase(choice)}
                        </option>
                      ))}
                    </select>
                  ) : type === 'float' ? (
                    <input
                      type="number"
                      step="1"
                      min={bounds[field].min}
                      max={bounds[field].max}
                      className="input"
                      value={draft[field]}
                      onChange={(e) => editor.setField(field, e.target.value)}
                    />
                  ) : (
                    <input
                      type="text"
                      className="input mono"
                      value={draft[field]}
                      onChange={(e) => editor.setField(field, e.target.value)}
                    />
                  )}
                </Field>
              ))}
            </div>
          </Card>

          <ConfigActionBar
            changeCount={editor.changeCount}
            reviewing={editor.reviewing}
            onReview={() => editor.review()}
            onDiscard={editor.discard}
          />

          <ReadOnlyCard title="API keys & hardcoded values" description={readOnly.description}>
            <dl>
              <div className="dl__row">
                <dt>OpenAI API key</dt>
                <dd>
                  <KeyStatus configured={readOnly.openai_api_key_configured} />
                </dd>
              </div>
              <div className="dl__row">
                <dt>Gemini API key</dt>
                <dd>
                  <KeyStatus configured={readOnly.gemini_api_key_configured} />
                </dd>
              </div>
              <div className="dl__row">
                <dt>Temperature</dt>
                <dd>
                  <span className="num">{readOnly.temperature}</span> <span className="muted small">— {readOnly.temperature_note}</span>
                </dd>
              </div>
            </dl>
          </ReadOnlyCard>
        </>
      )}
    </div>
  )
}
