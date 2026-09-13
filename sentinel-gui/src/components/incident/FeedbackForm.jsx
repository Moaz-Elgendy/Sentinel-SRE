import { useState } from 'react'
import { formatTimestamp, titleCase } from '../../utils/format.js'

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
export default function FeedbackForm({
  question,
  correctionLabel,
  options,
  history,
  onSubmit,
}) {
  const [answer, setAnswer] = useState(null) // true | false | null (not yet answered)
  const [correction, setCorrection] = useState('')
  const [note, setNote] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [submittedJustNow, setSubmittedJustNow] = useState(false)
  const [error, setError] = useState(null)

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
      setError('Could not save this feedback. Please try again.')
    } finally {
      setSubmitting(false)
    }
  }

  const canSubmit = answer === true || (answer === false && correction !== '')

  return (
    <div className="feedback-form">
      {history.length > 0 && (
        <div className="feedback-history">
          {history.map((row) => (
            <div key={row.id} className="feedback-history__row">
              <span className={row.correct_or_useful ? 'decision-tag decision-tag--allowed' : 'decision-tag decision-tag--denied'}>
                {row.correct_or_useful ? 'Confirmed' : 'Corrected'}
              </span>
              {row.corrected_value && <span className="mono">{titleCase(row.corrected_value)}</span>}
              {row.note && <span className="muted">"{row.note}"</span>}
              <span className="muted small">{formatTimestamp(row.created_at)}</span>
            </div>
          ))}
        </div>
      )}

      {submittedJustNow ? (
        <p className="muted">Thanks — feedback recorded.</p>
      ) : (
        <form onSubmit={handleSubmit}>
          <p>{question}</p>
          <div className="feedback-form__answer">
            <button
              type="button"
              className={`button ${answer === true ? 'button--primary' : 'button--ghost'}`}
              onClick={() => setAnswer(true)}
            >
              Yes
            </button>
            <button
              type="button"
              className={`button ${answer === false ? 'button--primary' : 'button--ghost'}`}
              onClick={() => setAnswer(false)}
            >
              No
            </button>
          </div>

          {answer === false && (
            <label className="field">
              <span>{correctionLabel}</span>
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
            </label>
          )}

          {answer !== null && (
            <label className="field">
              <span>Additional feedback (optional)</span>
              <textarea
                className="select feedback-form__note"
                value={note}
                onChange={(e) => setNote(e.target.value)}
                rows={2}
              />
            </label>
          )}

          {error && <p className="muted">{error}</p>}

          {answer !== null && (
            <button type="submit" className="button button--primary" disabled={!canSubmit || submitting}>
              {submitting ? 'Saving…' : 'Submit feedback'}
            </button>
          )}
        </form>
      )}
    </div>
  )
}
