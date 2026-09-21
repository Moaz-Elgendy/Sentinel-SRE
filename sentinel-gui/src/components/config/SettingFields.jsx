import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { textToList } from '@/utils/list'
import { SettingRow } from './ConfigShell.jsx'

const rangeHint = (bounds) => (bounds && bounds.min != null ? `Allowed range ${bounds.min} to ${bounds.max}` : undefined)

// Hints are sentences: join them cleanly whether or not the caller ended theirs with a full stop.
const joinHints = (...hints) => {
  const parts = hints.filter(Boolean).map((h) => h.replace(/\.$/, ''))
  return parts.length ? `${parts.join('. ')}.` : undefined
}

/** Numeric setting with the backend-supplied bounds as its hint (single source of truth). */
export function NumberSetting({ editor, field, label, step = '1', hint, extra }) {
  const bounds = editor.data.bounds?.[field]
  const id = `setting-${field}`
  return (
    <SettingRow label={label} htmlFor={id} modified={field in editor.changes} hint={joinHints(hint, rangeHint(bounds))}>
      <Input id={id} type="number" step={step} min={bounds?.min} max={bounds?.max} value={editor.draft[field]} onChange={(e) => editor.setField(field, e.target.value)} className="max-w-48 font-mono" />
      {extra}
    </SettingRow>
  )
}

export function TextSetting({ editor, field, label, hint, mono = true, placeholder }) {
  const id = `setting-${field}`
  return (
    <SettingRow label={label} htmlFor={id} modified={field in editor.changes} hint={hint}>
      <Input id={id} value={editor.draft[field]} onChange={(e) => editor.setField(field, e.target.value)} placeholder={placeholder} className={mono ? 'font-mono text-xs' : ''} />
    </SettingRow>
  )
}

export function SelectSetting({ editor, field, label, hint, format = (v) => v }) {
  const choices = editor.data.bounds?.[field]?.choices ?? []
  const id = `setting-${field}`
  return (
    <SettingRow label={label} htmlFor={id} modified={field in editor.changes} hint={hint}>
      <Select value={editor.draft[field]} onValueChange={(v) => editor.setField(field, v)}>
        <SelectTrigger id={id} className="w-full max-w-48">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {choices.map((choice) => (
            <SelectItem key={choice} value={choice}>
              {format(choice)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </SettingRow>
  )
}

/** Comma-separated set with a live preview of exactly what will be saved. */
export function ListSetting({ editor, field, label, hint }) {
  const id = `setting-${field}`
  const items = textToList(editor.draft[field] ?? '')
  return (
    <SettingRow label={label} htmlFor={id} modified={field in editor.changes} hint={hint}>
      <Input id={id} value={editor.draft[field]} onChange={(e) => editor.setField(field, e.target.value)} className="font-mono text-xs" />
      <ul className="mt-2 flex min-h-6 flex-wrap gap-1.5" aria-label={`${label} as parsed`}>
        {items.length === 0 ? (
          <li className="text-xs text-muted-foreground">Empty: Sentinel will not act on anything.</li>
        ) : (
          items.map((item) => (
            <li key={item}>
              <Badge variant="secondary" className="font-mono font-normal">
                {item}
              </Badge>
            </li>
          ))
        )}
      </ul>
    </SettingRow>
  )
}
