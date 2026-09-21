import { Activity, CircleAlert, CircleCheck, CircleHelp, Hand } from 'lucide-react'
import { useActivity } from '../context/ActivityContext.jsx'
import { useIncidentFeed } from '../context/IncidentFeedContext.jsx'
import { useSummary } from '../context/SummaryContext.jsx'
import { derivePosture } from '../utils/posture.js'

export const POSTURE_ICONS = { hand: Hand, alert: CircleAlert, activity: Activity, ok: CircleCheck, unknown: CircleHelp }

/** Shared so the topbar chip and the command-center banner can never disagree. */
export function usePosture() {
  const { data: summary, error: summaryError } = useSummary()
  const { awaiting, active, loading: feedLoading } = useIncidentFeed()
  const { status: activityStatus } = useActivity()
  const loading = feedLoading || (!summary && !summaryError)
  return derivePosture({ summary, summaryError, awaiting, active, activityStatus, loading })
}
