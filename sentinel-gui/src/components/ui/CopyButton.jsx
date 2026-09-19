import { useState } from 'react'
import Icon from './Icon.jsx'
import { useToast } from './Toast.jsx'

/** Copies a value (e.g. an incident id) and confirms it. */
export default function CopyButton({ value, label = 'Copy' }) {
  const toast = useToast()
  const [copied, setCopied] = useState(false)

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      toast(`Copied ${value}`, { duration: 2500 })
      setTimeout(() => setCopied(false), 1500)
    } catch {
      toast('Could not copy to the clipboard', { tone: 'error' })
    }
  }

  return (
    <button type="button" className="icon-button" onClick={handleCopy} aria-label={label} title={label}>
      <Icon name={copied ? 'check' : 'copy'} size={15} />
    </button>
  )
}
