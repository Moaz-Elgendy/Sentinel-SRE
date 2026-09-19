import { useId, useState } from 'react'
import { formatPercent, titleCase } from '../../utils/format.js'
import Icon from '../ui/Icon.jsx'
import Tag from '../ui/Tag.jsx'

/**
 * Renders `Incident.hypothesis` (see app/models/incident.py: Hypothesis).
 * Deliberately separate from EvidencePanel: this is Sentinel's
 * interpretation of the evidence, not the evidence itself.
 */
export default function ReasoningPanel({ hypothesis }) {
  const [expanded, setExpanded] = useState(false)
  const detailId = useId()

  if (!hypothesis) {
    return <p className="muted">No diagnosis yet.</p>
  }

  return (
    <div className="stack">
      <div className="reasoning__headline">
        <div>
          <div className="eyebrow">Diagnosis</div>
          <div className="reasoning__value">{titleCase(hypothesis.root_cause)}</div>
        </div>
        <div>
          <div className="eyebrow">Confidence</div>
          <div className="reasoning__value num">{formatPercent(hypothesis.confidence)}</div>
          {hypothesis.confidence != null && (
            <div
              className="meter"
              role="meter"
              aria-label="Confidence"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.round(hypothesis.confidence * 100)}
            >
              <div className="meter__fill" style={{ width: `${Math.round(hypothesis.confidence * 100)}%` }} />
            </div>
          )}
        </div>
        <div>
          <div className="eyebrow">Recommended action</div>
          <div className="reasoning__value">{titleCase(hypothesis.recommended_action)}</div>
        </div>
      </div>

      <div>
        <button
          type="button"
          className="link-button"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          aria-controls={detailId}
        >
          {expanded ? 'Hide reasoning' : 'Why?'}
          <Icon name={expanded ? 'chevronDown' : 'chevronRight'} size={14} />
        </button>

        {expanded && (
          <div className="reasoning__detail" id={detailId}>
            <p>{hypothesis.reasoning}</p>

            {hypothesis.supporting?.length > 0 && (
              <ul className="bullet-list">
                {hypothesis.supporting.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            )}

            <div className="cluster">
              <Tag tone="info">{hypothesis.source === 'rules+llm' ? 'Rules + LLM' : 'Rule-based'}</Tag>
              {hypothesis.rule_confidence != null && (
                <span className="muted small">
                  Rule-based confidence before LLM review: {formatPercent(hypothesis.rule_confidence)}
                </span>
              )}
            </div>
            {hypothesis.llm_note && <p className="muted reasoning__note">{hypothesis.llm_note}</p>}
          </div>
        )}
      </div>
    </div>
  )
}
