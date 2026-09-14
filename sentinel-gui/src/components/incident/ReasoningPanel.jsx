import { useState } from 'react'
import { formatPercent, titleCase } from '../../utils/format.js'

/**
 * Renders `Incident.hypothesis` (see app/models/incident.py: Hypothesis).
 * Deliberately separate from EvidencePanel: this is Sentinel's
 * interpretation of the evidence, not the evidence itself.
 */
export default function ReasoningPanel({ hypothesis }) {
  const [expanded, setExpanded] = useState(false)

  if (!hypothesis) {
    return <p className="muted">No diagnosis yet.</p>
  }

  return (
    <div className="reasoning-panel">
      <div className="reasoning-panel__headline">
        <div>
          <div className="reasoning-panel__label">Diagnosis</div>
          <div className="reasoning-panel__value">{titleCase(hypothesis.root_cause)}</div>
        </div>
        <div>
          <div className="reasoning-panel__label">Confidence</div>
          <div className="reasoning-panel__value">{formatPercent(hypothesis.confidence)}</div>
        </div>
        <div>
          <div className="reasoning-panel__label">Recommended action</div>
          <div className="reasoning-panel__value">{titleCase(hypothesis.recommended_action)}</div>
        </div>
      </div>

      <button type="button" className="link link--button" onClick={() => setExpanded((v) => !v)}>
        {expanded ? 'Hide reasoning ▲' : 'Why? ▼'}
      </button>

      {expanded && (
        <div className="reasoning-panel__detail">
          <p>{hypothesis.reasoning}</p>

          {hypothesis.supporting?.length > 0 && (
            <ul>
              {hypothesis.supporting.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          )}

          <div className="reasoning-panel__provenance">
            <span className="tag">{hypothesis.source === 'rules+llm' ? 'Rules + LLM' : 'Rule-based'}</span>
            {hypothesis.rule_confidence != null && (
              <span className="muted">
                Rule-based confidence before LLM review: {formatPercent(hypothesis.rule_confidence)}
              </span>
            )}
          </div>
          {hypothesis.llm_note && <p className="muted reasoning-panel__llm-note">{hypothesis.llm_note}</p>}
        </div>
      )}
    </div>
  )
}
