import { Fragment } from 'react'
import { applyRemediationChange, getRemediationConfig, previewRemediationChange } from '../api/config.js'
import { ChangeReview, LastChanged, ReadOnlyCard } from '../components/config/ConfigParts.jsx'
import AlertBanner from '../components/ui/AlertBanner.jsx'
import Button from '../components/ui/Button.jsx'
import Card from '../components/ui/Card.jsx'
import { ErrorState } from '../components/ui/EmptyState.jsx'
import Icon from '../components/ui/Icon.jsx'
import { PageSkeleton } from '../components/ui/Loading.jsx'
import PageHeader from '../components/ui/PageHeader.jsx'
import StatusPill from '../components/ui/StatusPill.jsx'
import { useConfigEditor } from '../hooks/useConfigEditor.js'
import { usePageTitle } from '../hooks/usePageTitle.js'
import { titleCase } from '../utils/format.js'

// This page has one editable setting (dry run) and it is changed through the
// same review → confirm flow as everything else, started from the toggle.
const makeDraft = (current) => ({ dry_run: current.dry_run })
const diffChanges = () => ({})

export default function RemediationConfigPage() {
  usePageTitle('Remediation')
  const editor = useConfigEditor({
    load: getRemediationConfig,
    previewChange: previewRemediationChange,
    applyChange: applyRemediationChange,
    makeDraft,
    diffChanges,
    loadErrorMessage: 'Could not load remediation configuration.',
  })
  const { data, preview } = editor

  if (editor.loading) return <PageSkeleton label="Loading remediation configuration…" cards={2} />
  if (editor.loadError) {
    return <ErrorState title="Couldn't load remediation configuration" message={editor.loadError} onRetry={editor.retryLoad} />
  }
  if (!data) return null

  const readOnly = data.read_only
  const dryRun = data.current.dry_run

  return (
    <div className="page">
      <PageHeader
        title="Remediation"
        subtitle={
          <>
            How Sentinel applies the actions it decides on.
            <LastChanged at={data.last_changed_at} by={data.last_changed_by} />
          </>
        }
      />

      {editor.justApplied && !preview && (
        <AlertBanner tone="success">Configuration applied — Sentinel is using the new value now.</AlertBanner>
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
          <Card
            title="Dry run mode"
            description="When on, Sentinel decides and authorises actions exactly as normal, but does not apply them to the cluster — everything else (evidence, RCA, Policy Engine, audit trail) runs for real."
          >
            <div className="mode-row">
              <div className="mode-row__state">
                <StatusPill
                  size="lg"
                  status={dryRun ? 'unknown' : 'escalated'}
                  label={dryRun ? 'Dry run — no cluster mutations' : 'Autonomous — actions are applied'}
                />
              </div>
              <Button
                variant={dryRun ? 'caution' : 'secondary'}
                onClick={() => editor.review({ dry_run: !dryRun })}
                busy={editor.reviewing}
                busyLabel="Validating…"
              >
                {dryRun ? 'Turn dry run off' : 'Turn dry run on'}
              </Button>
            </div>
          </Card>

          <ReadOnlyCard title="Action ladder" description={readOnly.description}>
            <div className="card card--flush table-wrap">
              <table className="table">
                <caption className="sr-only">Candidate remediation actions per root cause</caption>
                <thead>
                  <tr>
                    <th scope="col">Root cause</th>
                    <th scope="col">Candidate actions, in order</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(readOnly.action_ladder).map(([rootCause, actions]) => (
                    <tr key={rootCause}>
                      <td>{titleCase(rootCause)}</td>
                      <td>
                        {actions.length > 0 ? (
                          <span className="ladder">
                            {actions.map((action, i) => (
                              <Fragment key={action}>
                                {i > 0 && <Icon name="arrowRight" size={12} className="ladder__arrow" />}
                                <span className="tag">{titleCase(action)}</span>
                              </Fragment>
                            ))}
                          </span>
                        ) : (
                          <span className="muted">Never remediable</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="muted small ladder__footnote">
              Fallback confidence discount: <span className="num">{readOnly.fallback_confidence_discount}</span> per step down the
              ladder.
            </p>
          </ReadOnlyCard>
        </>
      )}
    </div>
  )
}
