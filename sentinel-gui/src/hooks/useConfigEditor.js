import { useCallback, useEffect, useState } from 'react'
import { extractErrorMessage } from '../api/client.js'

/**
 * The shared edit → review → apply flow behind every Administration page
 * (Policies, RCA, Remediation, AI, Monitoring). Each page supplies its own
 * API calls and its own draft/diff logic; this hook only owns the flow state,
 * so the five pages behave identically.
 *
 *   1. load()            GET the current config (+ bounds, read-only info)
 *   2. edit a draft      nothing is sent anywhere
 *   3. review()          POST /preview  — the backend validates and returns the diff
 *   4. apply()           POST /apply    — only after the admin confirms
 *
 * Arguments must be stable references (module-level functions).
 */
export function useConfigEditor({ load, previewChange, applyChange, makeDraft, diffChanges, loadErrorMessage }) {
  const [data, setData] = useState(null)
  const [draft, setDraft] = useState(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(null)

  const [preview, setPreview] = useState(null) // { valid, errors, diff, changes }
  const [reason, setReason] = useState('')
  const [reviewing, setReviewing] = useState(false)
  const [applying, setApplying] = useState(false)
  const [actionError, setActionError] = useState(null)
  const [justApplied, setJustApplied] = useState(false)

  const loadConfig = useCallback(() => {
    load()
      .then((body) => {
        setData(body)
        setDraft(makeDraft(body.current))
        setLoadError(null)
      })
      .catch((err) => setLoadError(extractErrorMessage(err, loadErrorMessage)))
      .finally(() => setLoading(false))
  }, [load, makeDraft, loadErrorMessage])

  useEffect(() => {
    loadConfig()
  }, [loadConfig])

  const changes = data && draft ? diffChanges(data.current, draft) : {}
  const changeCount = Object.keys(changes).length

  function setField(field, value) {
    setDraft((prev) => ({ ...prev, [field]: value }))
    setPreview(null)
    setJustApplied(false)
  }

  // `explicitChanges` lets a page start a review from something other than the
  // form draft (e.g. the dry-run toggle on the Remediation page).
  async function review(explicitChanges) {
    const toReview = explicitChanges ?? changes
    if (Object.keys(toReview).length === 0) return
    setReviewing(true)
    setActionError(null)
    try {
      const result = await previewChange(toReview)
      setPreview({ ...result, changes: toReview })
    } catch (err) {
      setActionError(extractErrorMessage(err, 'Could not validate these changes.'))
    } finally {
      setReviewing(false)
    }
  }

  async function apply() {
    setApplying(true)
    setActionError(null)
    try {
      await applyChange(preview.changes, reason)
      setPreview(null)
      setReason('')
      setJustApplied(true)
      loadConfig()
    } catch (err) {
      setActionError(extractErrorMessage(err, 'Could not apply these changes.'))
    } finally {
      setApplying(false)
    }
  }

  function cancelReview() {
    setPreview(null)
    setActionError(null)
  }

  function discard() {
    setDraft(makeDraft(data.current))
    setJustApplied(false)
    setActionError(null)
  }

  return {
    data,
    draft,
    loading,
    loadError,
    retryLoad: () => {
      setLoading(true)
      loadConfig()
    },
    preview,
    reason,
    setReason,
    reviewing,
    applying,
    actionError,
    justApplied,
    changes,
    changeCount,
    setField,
    review,
    apply,
    cancelReview,
    discard,
  }
}
