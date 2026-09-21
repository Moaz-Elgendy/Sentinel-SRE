import { useSyncExternalStore } from 'react'

// One shared 5-second ticker for every relative timestamp on screen. Hundreds
// of rows can show "3m ago" without hundreds of independent timers.
const listeners = new Set()
let timer = null
let tick = Date.now()

function subscribe(listener) {
  listeners.add(listener)
  if (!timer) {
    timer = setInterval(() => {
      tick = Date.now()
      listeners.forEach((l) => l())
    }, 5000)
  }
  return () => {
    listeners.delete(listener)
    if (listeners.size === 0) {
      clearInterval(timer)
      timer = null
    }
  }
}

const getSnapshot = () => tick

export function useTick() {
  return useSyncExternalStore(subscribe, getSnapshot)
}
