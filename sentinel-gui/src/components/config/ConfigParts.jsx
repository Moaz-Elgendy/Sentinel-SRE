import { titleCase } from '../../utils/format.js'
import AlertBanner from '../ui/AlertBanner.jsx'
import Button from '../ui/Button.jsx'
import Card from '../ui/Card.jsx'
import Field from '../ui/Field.jsx'
import Tag from '../ui/Tag.jsx'
import Timestamp from '../ui/Timestamp.jsx'

/** "Last changed 4m ago by admin" — the audit context every config page shows. */
export function LastChanged({ at, by }) {
  if (!at) return null
  return (
    <>
      {' '}
      Last changed <Timestamp epoch={at} /> by {by}.
    </>
  )
}

function formatChangeValue(value) {
  if (value == null) return '—'
  if (Array.isArray(value)) return value.join(', ') || '—'
  const text = String(value)
  return text === '' ? '(default)' : text
}

/**
 * Step 2 of every configuration change: the exact before/after diff, any
 * warnings from the backend's own validation, an audit reason, and an
 * explicit confirm. Nothing changes until "Confirm & Apply".
 */
export function ChangeReview({ preview, reason, onReasonChange, applying, onApply, onCancel }) {
  const hasErrors = preview.errors.length > 0
  const warnings = hasErrors ? [] : preview.diff.filter((d) => d.warning)

  return (
    <Card
      tone="warn"
      title={hasErrors ? "These changes can't be applied" : 'Review changes'}
      description={hasErrors ? undefined : 'Nothing is saved until you confirm. Applied changes take effect immediately and are recorded in the audit trail.'}
    >
      <div className="stack">
        {hasErrors ? (
          <>
            <AlertBanner title="Validation failed">
              <ul>
                {preview.errors.map((err) => (
                  <li key={err}>{err}</li>
                ))}
              </ul>
            </AlertBanner>
            <div>
              <Button onClick={onCancel}>Back to editing</Button>
            </div>
          </>
        ) : (
          <>
            <div className="table-wrap card card--flush">
              <table className="table">
                <caption className="sr-only">Pending changes</caption>
                <thead>
                  <tr>
                    <th scope="col">Setting</th>
                    <th scope="col">Current</th>
                    <th scope="col">New</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.diff.map((d) => (
                    <tr key={d.field}>
                      <td>{titleCase(d.field)}</td>
                      <td className="mono muted">{formatChangeValue(d.old_value)}</td>
                      <td className="mono diff__new">{formatChangeValue(d.new_value)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {warnings.map((d) => (
              <AlertBanner key={d.field} tone="warn">
                {d.warning}
              </AlertBanner>
            ))}

            <Field label="Reason" hint="Optional, but recommended — it is stored with the change in the audit trail.">
              <textarea className="textarea" rows={2} value={reason} onChange={(e) => onReasonChange(e.target.value)} />
            </Field>

            <div className="cluster">
              <Button variant="ghost" onClick={onCancel} disabled={applying}>
                Cancel
              </Button>
              <Button variant="primary" onClick={onApply} busy={applying} busyLabel="Applying…">
                Confirm &amp; apply
              </Button>
            </div>
          </>
        )}
      </div>
    </Card>
  )
}

/** Sticky footer for edit forms: how many unsaved edits, discard, review. */
export function ConfigActionBar({ changeCount, reviewing, onReview, onDiscard }) {
  const dirty = changeCount > 0
  return (
    <div className="action-bar" role="group" aria-label="Configuration changes">
      <span className="action-bar__status" aria-live="polite">
        {dirty ? (
          <>
            <Tag tone="info">{changeCount}</Tag> unsaved {changeCount === 1 ? 'change' : 'changes'}
          </>
        ) : (
          'No unsaved changes'
        )}
      </span>
      <span className="action-bar__actions">
        <Button variant="ghost" onClick={onDiscard} disabled={!dirty || reviewing}>
          Discard
        </Button>
        <Button variant="primary" onClick={onReview} disabled={!dirty} busy={reviewing} busyLabel="Validating…">
          Review changes
        </Button>
      </span>
    </div>
  )
}

/** Read-only settings are shown, clearly labelled as such, never as disabled inputs. */
export function ReadOnlyCard({ title, description, children }) {
  return (
    <Card title={title} description={description} actions={<Tag>Read-only</Tag>}>
      {children}
    </Card>
  )
}
