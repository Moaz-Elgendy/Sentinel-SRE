export const listToText = (list) => (list ?? []).join(', ')

export const textToList = (text) =>
  text
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
