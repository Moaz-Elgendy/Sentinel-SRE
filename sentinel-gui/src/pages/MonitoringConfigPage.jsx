import { applyMonitoringChange, getMonitoringConfig, previewMonitoringChange } from '../api/config.js'
import { ChangeReview, ConfigActionBar, LastChanged, ReadOnlyCard } from '../components/config/ConfigParts.jsx'
import AlertBanner from '../components/ui/AlertBanner.jsx'
import Card from '../components/ui/Card.jsx'
import { ErrorState } from '../components/ui/EmptyState.jsx'
import Field from '../components/ui/Field.jsx'
import { PageSkeleton } from '../components/ui/Loading.jsx'
import PageHeader from '../components/ui/PageHeader.jsx'
import StatusPill from '../components/ui/StatusPill.jsx'
import { useConfigEditor } from '../hooks/useConfigEditor.js'
import { usePageTitle } from '../hooks/usePageTitle.js'

// Bounds (min/max) come from the backend's /api/config/monitoring `bounds`
// payload — see monitoring_admin.py's TIMEOUT_BOUNDS, single source of truth.
const FIELDS = [
  { field: 'prometheus_url', label: 'Prometheus URL', type: 'string' },
  { field: 'prometheus_timeout_seconds', label: 'Prometheus timeout (seconds)', type: 'float' },
  { field: 'loki_url', label: 'Loki URL', type: 'string' },
  { field: 'loki_timeout_seconds', label: 'Loki timeout (seconds)', type: 'float' },
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

export default function MonitoringConfigPage() {
  usePageTitle('Monitoring')
  const editor = useConfigEditor({
    load: getMonitoringConfig,
    previewChange: previewMonitoringChange,
    applyChange: applyMonitoringChange,
    makeDraft,
    diffChanges,
    loadErrorMessage: 'Could not load monitoring configuration.',
  })
  const { data, draft, changes, preview } = editor

  if (editor.loading) return <PageSkeleton label="Loading monitoring configuration…" cards={2} />
  if (editor.loadError) {
    return <ErrorState title="Couldn't load monitoring configuration" message={editor.loadError} onRetry={editor.retryLoad} />
  }
  if (!data || !draft) return null

  const readOnly = data.read_only
  const k8s = readOnly.kubernetes

  return (
    <div className="page">
      <PageHeader
        title="Monitoring"
        subtitle={
          <>
            Prometheus and Loki connection settings.
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
          <Card title="Prometheus & Loki">
            <div className="form-grid">
              {FIELDS.map(({ field, label, type }) => (
                <Field
                  key={field}
                  label={label}
                  hint={type === 'float' ? `Allowed range ${data.bounds[field].min}–${data.bounds[field].max}` : undefined}
                  modified={field in changes}
                >
                  <input
                    type={type === 'float' ? 'number' : 'text'}
                    step={type === 'float' ? '1' : undefined}
                    min={type === 'float' ? data.bounds[field].min : undefined}
                    max={type === 'float' ? data.bounds[field].max : undefined}
                    className={type === 'float' ? 'input' : 'input mono'}
                    value={draft[field]}
                    onChange={(e) => editor.setField(field, e.target.value)}
                  />
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

          <ReadOnlyCard title="Kubernetes connection" description={readOnly.description}>
            <dl>
              <div className="dl__row">
                <dt>Mode</dt>
                <dd>{k8s.mode}</dd>
              </div>
              <div className="dl__row">
                <dt>Namespace</dt>
                <dd className="mono">{k8s.namespace}</dd>
              </div>
              <div className="dl__row">
                <dt>Reachable</dt>
                <dd>
                  <StatusPill
                    status={k8s.available ? 'operational' : 'critical'}
                    label={k8s.available ? 'Yes' : `No${k8s.init_error ? ` — ${k8s.init_error}` : ''}`}
                  />
                </dd>
              </div>
              <div className="dl__row">
                <dt>Prometheus bearer token configured</dt>
                <dd>{readOnly.prometheus_bearer_token_configured ? 'Yes' : 'No'}</dd>
              </div>
              <div className="dl__row">
                <dt>Loki bearer token configured</dt>
                <dd>{readOnly.loki_bearer_token_configured ? 'Yes' : 'No'}</dd>
              </div>
            </dl>
          </ReadOnlyCard>

          <ReadOnlyCard title="Health checks" description={readOnly.health_checks.description}>
            <p className="muted">{readOnly.health_checks.note}</p>
          </ReadOnlyCard>
        </>
      )}
    </div>
  )
}
