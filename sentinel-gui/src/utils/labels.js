import { sentenceCase } from './format.js'

// Wording for the backend's enums (sentinel-ai/app/models/incident.py and
// lifecycle/policy.py). Unknown values fall back to a readable sentence-case
// version of the raw key, so a new backend value never renders blank.

export const ESCALATION_REASON = {
  no_safe_action: 'No safe autonomous action',
  action_cap_reached: 'Action limit reached',
  validation_failed: 'Recovery validation failed',
  stateful_target: 'Target is a stateful workload',
  remediation_error: 'Remediation errored',
  low_confidence: 'Confidence too low to act',
  unknown_alert: 'Unrecognised alert',
  internal_error: 'Internal error',
  lifecycle_timeout: 'Investigation timed out',
  retry_limit_reached: 'Retry limit reached',
  interrupted_by_restart: 'Sentinel restarted mid-incident',
}

export const DENIAL_REASON = {
  namespace_not_allowed: 'Namespace isn’t on the allow-list',
  namespace_frozen_deny: 'Namespace is protected',
  deployment_not_allowed: 'Deployment isn’t on the allow-list',
  deployment_frozen_deny: 'Deployment is protected',
  stateful_target: 'Stateful workload, never remediated',
  confidence_too_low: 'Confidence below the required threshold',
  no_previous_revision: 'No earlier revision to roll back to',
  no_deployment_history: 'No deployment history available',
  no_deploy_correlation: 'No recent deployment correlates',
  not_reversible: 'Action isn’t reversible',
  validation_unavailable: 'Recovery can’t be validated',
  replicas_out_of_band: 'Replica count outside the allowed range',
  action_cap_reached: 'Per-incident action limit reached',
  cooldown_active: 'Cooldown still active',
  already_attempted: 'Already attempted',
  missing_target: 'No target identified',
  no_chaos_surface: 'No chaos control surface',
  unknown_action: 'Unknown action',
}

// Policy Engine checks (lifecycle/policy.py). `true` = passed.
export const POLICY_CHECK = {
  namespace_allowed: 'Namespace is on the allow-list',
  namespace_not_frozen_denied: 'Namespace is not protected',
  deployment_allowed: 'Deployment is on the allow-list',
  deployment_not_frozen_denied: 'Deployment is not protected',
  target_not_stateful: 'Target is not a database or stateful workload',
  action_cap: 'Under the per-incident action limit',
  cooldown: 'Cooldown since the last action has elapsed',
  confidence: 'Confidence meets the threshold',
  confidence_human_override: 'Confidence check waived by SRE authorization',
  previous_revision_exists: 'An earlier revision exists',
  deployment_history: 'Deployment history is available',
  deploy_correlation: 'A recent deployment correlates',
  within_correlation_window: 'Deployment is inside the correlation window',
  reversible: 'Action is reversible',
  validation_available: 'Recovery can be validated afterwards',
  replicas_specified: 'Replica target is specified',
  replicas_not_zero: 'Replica target is above zero',
  replicas_in_band: 'Replica target is within the allowed range',
  chaos_surface: 'Chaos control surface is reachable',
}

export const VALIDATION_OUTCOME = {
  passed: 'Recovery confirmed',
  failed: 'Recovery not confirmed',
  timeout: 'Validation timed out',
  degraded: 'Partially recovered',
  unavailable: 'Validation unavailable',
}

export const LLM_STATUS = {
  not_configured: 'LLM not configured, rules only',
  ok: 'LLM consulted',
  reasoner_unavailable: 'LLM unavailable, rules only',
  disabled: 'LLM disabled, rules only',
}

export const escalationReasonLabel = (key) => ESCALATION_REASON[key] ?? sentenceCase(key)
export const denialReasonLabel = (key) => DENIAL_REASON[key] ?? sentenceCase(key)
export const policyCheckLabel = (key) => POLICY_CHECK[key] ?? sentenceCase(key)
export const llmStatusLabel = (key) => LLM_STATUS[key] ?? sentenceCase(key)
