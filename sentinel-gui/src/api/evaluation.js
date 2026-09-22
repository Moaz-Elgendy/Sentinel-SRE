import { client } from './client.js'

/** Aggregate ground-truth accuracy across chaos-scenario evaluation runs
 * (lifecycle/evaluation.py). Resolves any still-pending runs against the
 * incident store first, so this reflects the latest known outcome. */
export function getEvaluationSummary() {
  return client.get('/api/evaluation/summary').then((r) => r.data)
}

/** Every recorded evaluation run, most-recently-resolved state included. */
export function getEvaluationRuns() {
  return client.get('/api/evaluation/runs').then((r) => r.data)
}
