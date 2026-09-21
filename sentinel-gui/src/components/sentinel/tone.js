/** Semantic tones → Tailwind classes. The only place hue is mapped to meaning. */
export const TONE_TEXT = {
  ok: 'text-ok',
  warn: 'text-warn',
  bad: 'text-bad',
  info: 'text-info',
  neutral: 'text-neutral',
}

export const TONE_SOLID = {
  ok: 'bg-ok-solid',
  warn: 'bg-warn-solid',
  bad: 'bg-bad-solid',
  info: 'bg-info-solid',
  neutral: 'bg-neutral-solid',
}

export const TONE_SURFACE = {
  ok: 'border-ok-edge bg-ok-tint',
  warn: 'border-warn-edge bg-warn-tint',
  bad: 'border-bad-edge bg-bad-tint',
  info: 'border-info-edge bg-info-tint',
  neutral: 'border-border bg-muted/40',
}

export const TONE_BORDER_L = {
  ok: 'border-l-ok-solid',
  warn: 'border-l-warn-solid',
  bad: 'border-l-bad-solid',
  info: 'border-l-info-solid',
  neutral: 'border-l-neutral-solid',
}
