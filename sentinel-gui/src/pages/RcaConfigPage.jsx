import { Link } from 'react-router-dom'
import { applyRcaChange, getRcaConfig, previewRcaChange } from '../api/config.js'
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

const FIELDS = [
  { field: 'max_error_rate', label: 'Max error rate (correlation + validation)' },
  { field: 'max_p95_seconds', label: 'Max P95 latency (seconds)' },
  { field: 'max_cpu_cores', label: 'Max CPU (cores)' },
  { field: 'max_memory_bytes', label: 'Max memory (bytes)' },
  { field: 'settle_seconds', label: 'Settle period before validating (seconds)' },
  { field: 'timeout_seconds', label: 'Validation timeout (seconds)' },
  { field: 'poll_interval_seconds', label: 'Validation poll interval (seconds)' },
]

function makeDraft(current) {
  return Object.fromEntries(FIELDS.map(({ field }) => [field, current[field]]))
}

function diffChanges(current, draft) {
  const changes = {}
  for (const { field } of FIELDS) {
    const draftValue = Number(draft[field])
    if (!Number.isNaN(draftValue) && draftValue !== current[field]) {
      changes[field] = draftValue
    }
  }
  return changes
}

export default function RcaConfigPage() {
  usePageTitle('RCA & Diagnosis')
  const editor = useConfigEditor({
    load: getRcaConfig,
    previewChange: previewRcaChange,
    applyChange: applyRcaChange,
    makeDraft,
    diffChanges,
    loadErrorMessage: 'Could not load RCA configuration.',
  })
  const { data, draft, changes, preview } = editor

  if (editor.loading) return <PageSkeleton label="Loading RCA configuration…" cards={2} />
  if (editor.loadError) {
    return <ErrorState title="Couldn't load RCA configuration" message={editor.loadError} onRetry={editor.retryLoad} />
  }
  if (!data || !draft) return null

  const readOnly = data.read_only

  return (
    <div className="page">
      <PageHeader
        title="RCA & Diagnosis"
        subtitle={
          <>
            Evidence and recovery-validation thresholds — live on Sentinel's real correlation and validation steps.
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
          <Card title="Evidence & validation thresholds">
            <div className="form-grid">
              {FIELDS.map(({ field, label }) => (
                <Field
                  key={field}
                  label={label}
                  hint={`Allowed range ${data.bounds[field].min}–${data.bounds[field].max}`}
                  modified={field in changes}
                >
                  <input
                    type="number"
                    step={field === 'max_error_rate' ? '0.01' : '1'}
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

          <ConfigActionBar
            changeCount={editor.changeCount}
            reviewing={editor.reviewing}
            onReview={() => editor.review()}
            onDiscard={editor.discard}
          />

          <ReadOnlyCard title="What controls Sentinel's diagnosis" description={readOnly.rule_based_detection.description}>
            <dl>
              <div className="dl__row">
                <dt>LLM confidence ceiling</dt>
                <dd className="num">{readOnly.rule_based_detection.llm_confidence_ceiling}</dd>
              </div>
              <div className="dl__row">
                <dt>LLM confidence delta cap</dt>
                <dd className="num">{readOnly.rule_based_detection.llm_confidence_delta_cap}</dd>
              </div>
              <div className="dl__row">
                <dt>Rule confidence max</dt>
                <dd className="num">{readOnly.rule_based_detection.rule_confidence_max}</dd>
              </div>
              <div className="dl__row">
                <dt>Deployment correlation window</dt>
                <dd>
                  {readOnly.deployment_correlation_window_minutes.value} min — edit via{' '}
                  <Link to="/policies" className="link">
                    Policies
                  </Link>
                </dd>
              </div>
            </dl>

            <h3 className="section-label section-label--spaced">Root cause taxonomy</h3>
            <div className="tag-list">
              {readOnly.root_causes.map((rc) => (
                <Tag key={rc}>{titleCase(rc)}</Tag>
              ))}
            </div>
          </ReadOnlyCard>
        </>
      )}
    </div>
  )
}
