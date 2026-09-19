import { cloneElement, useId } from 'react'

/**
 * Label + control + hint. The control is passed as the single child and is
 * wired up with id / aria-describedby here, so every field is properly
 * labelled. `modified` flags a value that differs from what is saved.
 */
export default function Field({ label, hint, modified = false, className = '', children }) {
  const id = useId()
  const hintId = `${id}-hint`
  return (
    <div className={`field${modified ? ' field--modified' : ''}${className ? ` ${className}` : ''}`}>
      <label className="field__label" htmlFor={id}>
        {label}
        {modified && <span className="field__modified">Modified</span>}
      </label>
      {cloneElement(children, { id, 'aria-describedby': hint ? hintId : undefined })}
      {hint && (
        <span className="field__hint" id={hintId}>
          {hint}
        </span>
      )}
    </div>
  )
}
