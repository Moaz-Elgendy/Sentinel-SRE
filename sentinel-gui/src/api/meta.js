import { client } from './client.js'

export function listRootCauses() {
  return client.get('/api/meta/root-causes').then((r) => r.data.root_causes)
}

export function listActionTypes() {
  return client.get('/api/meta/actions').then((r) => r.data.actions)
}

export function getLifecyclePhases() {
  return client.get('/api/meta/lifecycle-phases').then((r) => r.data)
}
