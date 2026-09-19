import { useEffect, useId, useRef } from 'react'
import Button from './Button.jsx'

/**
 * Accessible replacement for window.confirm. Built on the native <dialog>, so
 * focus is trapped, Escape closes it and the page behind is inert. Cancel comes
 * first in the DOM, so it receives initial focus — the safe default for
 * anything consequential.
 */
export default function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel = 'Continue',
  cancelLabel = 'Cancel',
  tone = 'primary',
  busy = false,
  onConfirm,
  onCancel,
}) {
  const ref = useRef(null)
  const titleId = useId()

  useEffect(() => {
    const dialog = ref.current
    if (!dialog) return
    if (open && !dialog.open) dialog.showModal()
    if (!open && dialog.open) dialog.close()
  }, [open])

  return (
    <dialog
      ref={ref}
      className="dialog"
      aria-labelledby={titleId}
      onCancel={(event) => {
        event.preventDefault()
        onCancel()
      }}
      onClick={(event) => {
        // A click on the backdrop targets the <dialog> element itself.
        if (event.target === ref.current) onCancel()
      }}
    >
      <div className="dialog__body">
        <h2 className="dialog__title" id={titleId}>
          {title}
        </h2>
        <div className="dialog__content">{children}</div>
        <div className="dialog__actions">
          <Button variant="ghost" onClick={onCancel} disabled={busy}>
            {cancelLabel}
          </Button>
          <Button variant={tone} onClick={onConfirm} busy={busy}>
            {confirmLabel}
          </Button>
        </div>
      </div>
    </dialog>
  )
}
