import { applyPolicyChange, getPolicyConfig, previewPolicyChange } from '../api/config.js'
import { ChangeReview, ConfigActionBar, LastChanged, ReadOnlyCard } from '../components/config/ConfigParts.jsx'
import AlertBanner from '../components/ui/AlertBanner.jsx'
import Card from '../components/ui/Card.jsx'
import { ErrorState } from '../components/ui/EmptyState.jsx'
import Field from '../components/ui/Field.jsx'
import { PageSkeleton } from '../components/ui/Loading.jsx'
import PageHeader from '../components/ui/PageHeader.jsx'
import { useConfigEditor } from '../hooks/useConfigEditor.js'
import { usePageTitle } from '../hooks/usePageTitle.js'

const CONFIDENCE_FIELDS = [
  { field: 'confidence_restart', label: 'Restart' },
  { field: 'confidence_rollback', label: 'Rollback' },
  { field: 'confidence_scale', label: 'Scale' },
  { field: 'confidence_chaos_reset', label: 'Chaos reset' },
]

const LIMIT_FIELDS = [
  { field: 'min_replicas', label: 'Minimum replicas' },
  { field: 'max_replicas', label: 'Maximum replicas' },
  { field: 'max_actions_per_incident', label: 'Max actions per incident' },
  { field: 'action_cooldown_seconds', label: 'Action cooldown (seconds)' },
  { field: 'deployment_correlation_window_minutes', label: 'Deploy correlation window (minutes)' },
]

const LIST_FIELDS = [
  { field: 'allowed_namespaces', label: 'Allowed namespaces' },
  { field: 'allowed_deployments', label: 'Allowed deployments' },
]

function listToText(list) {
  return (list ?? []).join(', ')
}

function textToList(text) {
  return text
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
}

function makeDraft(current) {
  return {
    ...Object.fromEntries([...CONFIDENCE_FIELDS, ...LIMIT_FIELDS].map(({ field }) => [field, current[field]])),
    ...Object.fromEntries(LIST_FIELDS.map(({ field }) => [field, listToText(current[field])])),
  }
}

function diffChanges(current, draft) {
  const changes = {}
  for (const { field } of [...CONFIDENCE_FIELDS, ...LIMIT_FIELDS]) {
    const draftValue = Number(draft[field])
    if (!Number.isNaN(draftValue) && draftValue !== current[field]) {
      changes[field] = draftValue
    }
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

function rangeHint(bounds) {
  return bounds ? `Allowed range ${bounds.min}–${bounds.max}` : undefined
}

export default function PoliciesPage() {
  usePageTitle('Policies')
  const editor = useConfigEditor({
    load: getPolicyConfig,
    previewChange: previewPolicyChange,
    applyChange: applyPolicyChange,
    makeDraft,
    diffChanges,
    loadErrorMessage: 'Could not load policy configuration.',
  })
  const { data, draft, changes, preview } = editor

  if (editor.loading) return <PageSkeleton label="Loading policy configuration…" cards={3} />
  if (editor.loadError) {
    return <ErrorState title="Couldn't load policies" message={editor.loadError} onRetry={editor.retryLoad} />
  }
  if (!data || !draft) return null

  return (
    <div className="page">
      <PageHeader
        title="Policies"
        subtitle={
          <>
            Backed live by Sentinel's real Policy Engine — a change here takes effect on the very next incident
            evaluation.
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
          <Card title="Confidence thresholds" description="Minimum diagnosis confidence required before Sentinel acts autonomously.">
            <div className="form-grid">
              {CONFIDENCE_FIELDS.map(({ field, label }) => (
                <Field key={field} label={label} hint={rangeHint(data.bounds[field])} modified={field in changes}>
                  <input
                    type="number"
                    step="0.01"
                    min={data.bounds[field].min}
                    max={data.bounds[field].max}
                    className="input"
                    value={draft[field]}
                    onChange={(e) => editor.setField(field, e.target.value)}
                  />
                </Field>
              ))}
            </div>
          </Card>

          <Card title="Limits & cooldowns">
            <div className="form-grid">
              {LIMIT_FIELDS.map(({ field, label }) => (
                <Field key={field} label={label} hint={rangeHint(data.bounds[field])} modified={field in changes}>
                  <input
                    type="number"
                    min={data.bounds[field].min}
                    max={data.bounds[field].max}
                    className="input"
                    value={draft[field]}
                    onChange={(e) => editor.setField(field, e.target.value)}
                  />
                </Field>
              ))}
            </div>
          </Card>

          <Card
            title="Allow-lists"
            description="Comma-separated. An entry here can never include anything on the protected list below."
          >
            <div className="form-grid form-grid--1">
              {LIST_FIELDS.map(({ field, label }) => (
                <Field key={field} label={label} modified={field in changes}>
                  <input
                    type="text"
                    className="input mono"
                    value={draft[field]}
                    onChange={(e) => editor.setField(field, e.target.value)}
                  />
                </Field>
              ))}
            </div>
          </Card>

          <ReadOnlyCard title="Protected — never editable, here or anywhere else">
            <dl>
              <div className="dl__row">
                <dt>Denied deployments</dt>
                <dd className="mono">{data.protected.denied_deployments.join(', ') || '—'}</dd>
              </div>
              <div className="dl__row">
                <dt>Denied namespaces</dt>
                <dd className="mono">{data.protected.denied_namespaces.join(', ') || '—'}</dd>
              </div>
            </dl>
          </ReadOnlyCard>

          <ConfigActionBar
            changeCount={editor.changeCount}
            reviewing={editor.reviewing}
            onReview={() => editor.review()}
            onDiscard={editor.discard}
          />
        </>
      )}
    </div>
  )
}
