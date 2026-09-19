import { useId, useState } from 'react'
import { formatTimestamp, titleCase } from '../../utils/format.js'
import AlertBanner from '../ui/AlertBanner.jsx'
import Button from '../ui/Button.jsx'
import Field from '../ui/Field.jsx'
import Tag from '../ui/Tag.jsx'

/**
 * One feedback form (diagnosis or remediation) plus the history of
 * feedback already submitted for this incident. Options for "what should
 * it have been instead" are sourced from the real backend enums (see
 * api/meta.js -> GET /api/meta/root-causes /-/actions) rather than a
 * hand-typed list — this caught a real bug during backend testing where a
 * plausible-looking guess ("high_cpu") was not actually one of Sentinel's
 * root causes ("cpu_saturation" is), which is exactly the drift this
 * design avoids for the SRE actually using the form.
 *
 * This is a data-collection form only. Submitting feedback here does not
 * change Sentinel's behavior on this or any future incident — see
 * sentinel-ai/app/routers/feedback.py's module docstring.
 */
export default function FeedbackForm({ question, correctionLabel, options, history, onSubmit }) {
  const [answer, setAnswer] = useState(null) // true | false | null (not yet answered)
  const [correction, setCorrection] = useState('')
  const [note, setNote] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [submittedJustNow, setSubmittedJustNow] = useState(false)
  const [error, setError] = useState(null)
  const questionId = useId()

  async function handleSubmit(event) {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await onSubmit({ answer, correction: correction || null, note: note || null })
      setSubmittedJustNow(true)
      setAnswer(null)
      setCorrection('')
      setNote('')
    } catch {
      setError('Could not save this feedback. Your answer is still here — please try again.')
    } finally {
      setSubmitting(false)
    }
  }

  const canSubmit = answer === true || (answer === false && correction !== '')

  return (
    <div className="stack">
      {history.length > 0 && (
        <ul className="feedback-history">
          {history.map((row) => (
            <li key={row.id}>
              <Tag tone={row.correct_or_useful ? 'ok' : 'bad'}>{row.correct_or_useful ? 'Confirmed' : 'Corrected'}</Tag>
              {row.corrected_value && <span className="mono">{titleCase(row.corrected_value)}</span>}
              {row.note && <span className="muted">“{row.note}”</span>}
              <span className="muted small">{formatTimestamp(row.created_at)}</span>
            </li>
          ))}
        </ul>
      )}

      {submittedJustNow ? (
        <AlertBanner tone="success">Thanks — feedback recorded.</AlertBanner>
      ) : (
        <form onSubmit={handleSubmit} className="stack">
          <div className="feedback-question">
            <span id={questionId}>{question}</span>
            <div className="segmented" role="group" aria-labelledby={questionId}>
              <button type="button" className="segmented__option" aria-pressed={answer === true} onClick={() => setAnswer(true)}>
                Yes
              </button>
              <button type="button" className="segmented__option" aria-pressed={answer === false} onClick={() => setAnswer(false)}>
                No
              </button>
            </div>
          </div>

          {answer === false && (
            <Field label={correctionLabel}>
              <select className="select" value={correction} onChange={(e) => setCorrection(e.target.value)} required>
                <option value="" disabled>
                  Select one…
                </option>
                {options.map((opt) => (
                  <option key={opt} value={opt}>
                    {titleCase(opt)}
                  </option>
                ))}
              </select>
            </Field>
          )}

          {answer !== null && (
            <Field label="Additional feedback (optional)">
              <textarea className="textarea" value={note} onChange={(e) => setNote(e.target.value)} rows={2} />
            </Field>
          )}

          {error && <AlertBanner>{error}</AlertBanner>}

          {answer !== null && (
            <div>
              <Button type="submit" variant="primary" disabled={!canSubmit} busy={submitting} busyLabel="Saving…">
                Submit feedback
              </Button>
            </div>
          )}
        </form>
      )}
    </div>
  )
}
