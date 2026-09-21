import { LoaderCircle } from 'lucide-react'
import { useCallback, useEffect, useId, useState } from 'react'
import { extractErrorMessage } from '@/api/client'
import { listIncidentFeedback, submitDiagnosisFeedback, submitRemediationFeedback } from '@/api/feedback'
import { listActionTypes, listRootCauses } from '@/api/meta'
import { Panel } from '@/components/sentinel/Panel'
import { Callout } from '@/components/sentinel/States'
import { Timestamp } from '@/components/sentinel/Timestamp'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { CircleCheck } from 'lucide-react'
import { actionLabel, rootCauseLabel } from '@/utils/incident'

function FeedbackForm({ question, correctionLabel, options, format, history, onSubmit }) {
  const [answer, setAnswer] = useState(null) // 'yes' | 'no' | null
  const [correction, setCorrection] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState(false)
  const [error, setError] = useState(null)
  const questionId = useId()
  const canSubmit = answer === 'yes' || (answer === 'no' && correction !== '')

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await onSubmit({ answer: answer === 'yes', correction: correction || null, note: note || null })
      setDone(true)
      setAnswer(null)
      setCorrection('')
      setNote('')
    } catch (err) {
      setError(extractErrorMessage(err, 'Could not save this feedback. Your answer is still here, so try again.'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-3">
      {history.length > 0 && (
        <ul className="divide-y rounded-md border">
          {history.map((row) => (
            <li key={row.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-sm">
              <Badge variant={row.correct_or_useful ? 'ok' : 'warn'}>{row.correct_or_useful ? 'Confirmed' : 'Corrected'}</Badge>
              {row.corrected_value && <span className="font-medium">{format(row.corrected_value)}</span>}
              {row.note && <span className="text-muted-foreground">“{row.note}”</span>}
              <span className="ml-auto text-xs text-muted-foreground">
                <Timestamp value={row.created_at} />
              </span>
            </li>
          ))}
        </ul>
      )}
      {done && (
        <Callout tone="ok" icon={CircleCheck}>
          Thanks, feedback recorded.
        </Callout>
      )}
      <form onSubmit={submit} className="space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <span id={questionId} className="text-sm">
            {question}
          </span>
          <ToggleGroup type="single" variant="outline" size="sm" value={answer ?? ''} onValueChange={(v) => { setAnswer(v || null); setDone(false) }} aria-labelledby={questionId}>
            <ToggleGroupItem value="yes" className="px-3">Yes</ToggleGroupItem>
            <ToggleGroupItem value="no" className="px-3">No</ToggleGroupItem>
          </ToggleGroup>
        </div>
        {answer === 'no' && (
          <div className="space-y-1.5">
            <Label>{correctionLabel}</Label>
            <Select value={correction} onValueChange={setCorrection}>
              <SelectTrigger className="w-full max-w-72" aria-label={correctionLabel}>
                <SelectValue placeholder="Select one…" />
              </SelectTrigger>
              <SelectContent>
                {options.map((option) => (
                  <SelectItem key={option} value={option}>
                    {format(option)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )}
        {answer !== null && (
          <div className="space-y-1.5">
            <Label htmlFor={`${questionId}-note`}>Additional feedback (optional)</Label>
            <Textarea id={`${questionId}-note`} value={note} onChange={(e) => setNote(e.target.value)} rows={2} className="max-w-xl" />
          </div>
        )}
        {error && <Callout tone="bad">{error}</Callout>}
        {answer !== null && (
          <Button type="submit" disabled={!canSubmit || busy}>
            {busy && <LoaderCircle className="animate-spin" />} Submit feedback
          </Button>
        )}
      </form>
    </div>
  )
}

/** How accurate was Sentinel? Data collection only — it never changes Sentinel's behaviour. */
export function FeedbackTab({ incident }) {
  const [feedback, setFeedback] = useState([])
  const [rootCauses, setRootCauses] = useState([])
  const [actions, setActions] = useState([])

  const refresh = useCallback(() => {
    listIncidentFeedback(incident.id)
      .then(setFeedback)
      .catch(() => setFeedback([]))
  }, [incident.id])

  useEffect(() => {
    refresh()
    // Options come from the backend's own enums so they can never drift from Sentinel's real vocabulary.
    listRootCauses().then(setRootCauses).catch(() => {})
    listActionTypes().then(setActions).catch(() => {})
  }, [refresh])

  return (
    <div className="space-y-4">
      <p className="max-w-2xl text-sm text-muted-foreground">Feedback measures how accurate Sentinel was. Submitting it does not change how Sentinel behaves on this or any future incident.</p>
      <div className="grid gap-4 xl:grid-cols-2">
        <Panel title="Was the diagnosis right?" description={incident.hypothesis ? `Sentinel said: ${rootCauseLabel(incident.hypothesis.root_cause)}` : undefined}>
          <FeedbackForm
            question="Was Sentinel’s diagnosis correct?"
            correctionLabel="What was the actual root cause?"
            options={rootCauses}
            format={rootCauseLabel}
            history={feedback.filter((f) => f.kind === 'diagnosis')}
            onSubmit={({ answer, correction, note }) => submitDiagnosisFeedback(incident.id, { correct: answer, actualRootCause: correction, note }).then(refresh)}
          />
        </Panel>
        <Panel title="Was the remediation useful?" description="Whether the action Sentinel took (or proposed) was the right one">
          <FeedbackForm
            question="Was the remediation useful?"
            correctionLabel="What should Sentinel have done instead?"
            options={actions}
            format={actionLabel}
            history={feedback.filter((f) => f.kind === 'remediation')}
            onSubmit={({ answer, correction, note }) => submitRemediationFeedback(incident.id, { useful: answer, suggestedAction: correction, note }).then(refresh)}
          />
        </Panel>
      </div>
    </div>
  )
}
