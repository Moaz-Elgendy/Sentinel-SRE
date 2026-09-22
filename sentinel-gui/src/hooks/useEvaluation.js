import { getEvaluationRuns, getEvaluationSummary } from '../api/evaluation.js'
import { usePolling } from './usePolling.js'

async function fetchEvaluation() {
  const [summary, runsResponse] = await Promise.all([getEvaluationSummary(), getEvaluationRuns()])
  return { summary, runs: runsResponse.runs }
}

/**
 * Chaos-scenario ground-truth evaluation (lifecycle/evaluation.py): the
 * aggregate rca_correctness_rate/decision_accuracy_rate plus the individual
 * runs behind them. Both endpoints resolve any still-pending run against the
 * incident store lazily, at read time — polling here is what keeps a run's
 * status current, the same reason usePerformance polls.
 */
export function useEvaluation(intervalMs = 20000) {
  return usePolling(fetchEvaluation, { intervalMs })
}
