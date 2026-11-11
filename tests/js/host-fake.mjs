// Recording fake of the SDK `host` singleton plus a tiny nanostores-compatible atom.
// Tests drive it through the helpers exported here (the plugin only sees `host`).

export function atom(initial) {
  let value = initial
  const listeners = new Set()

  const store = {
    get: () => value,
    set(next) {
      value = next
      listeners.forEach(listener => listener(value))
    },
    setKey(key, next) {
      store.set({ ...value, [key]: next })
    },
    listen(listener) {
      listeners.add(listener)

      return () => listeners.delete(listener)
    },
    subscribe(listener) {
      listener(value)

      return store.listen(listener)
    },
    get value() {
      return value
    }
  }

  return store
}

export function computed(stores, fn) {
  const list = Array.isArray(stores) ? stores : [stores]
  const read = () => fn(...list.map(store => store.get()))

  return {
    get: read,
    listen: listener => {
      const offs = list.map(store => store.listen(() => listener(read())))

      return () => offs.forEach(off => off())
    },
    subscribe(listener) {
      listener(read())

      return this.listen(listener)
    },
    get value() {
      return read()
    }
  }
}

const readonly = store => ({ get: store.get, listen: store.listen, subscribe: store.subscribe })

/** Every call into the fake host, in order: { method, args }. */
export const hostCalls = []

const record = (method, result) => (...args) => {
  hostCalls.push({ method, args })

  return typeof result === 'function' ? result(...args) : result
}

const stateAtoms = {
  activeSessionId: atom(null),
  awaitingResponse: atom(false),
  busy: atom(false),
  busyBySession: atom({}),
  connectionId: atom('local'),
  cwd: atom(''),
  focusedSessionId: atom(null),
  focusedSessionOwner: atom(null),
  focusedSessionProfile: atom('default'),
  focusedStoredSessionId: atom(null),
  focusedUsage: atom(null),
  gateway: atom('open'),
  model: atom('test-model'),
  profile: atom('default'),
  viewport: atom({ width: 1280, height: 800, narrow: false })
}

export const hostStateAtoms = stateAtoms

const openWorkspaceImpl = (id, options) => {
  hostCalls.push({ method: 'openWorkspace', args: [id, options] })

  if (!String(id ?? '').trim() || typeof options?.render !== 'function') {
    throw new Error('openWorkspace: an id and a render function are required')
  }

  return () => hostCalls.push({ method: 'openWorkspace.close', args: [id] })
}

let notifyCounter = 0

export const host = {
  state: Object.fromEntries(Object.entries(stateAtoms).map(([key, store]) => [key, readonly(store)])),
  settings: { get: record('settings.get', undefined), set: record('settings.set', undefined) },
  notify: (input) => {
    hostCalls.push({ method: 'notify', args: [input] })
    notifyCounter += 1

    return input?.id ?? `notification-${notifyCounter}`
  },
  notifyError: record('notifyError', 'notification-error'),
  logs: record('logs', Promise.resolve('')),
  completeMcpOAuth: record('completeMcpOAuth', Promise.resolve({})),
  navigate: record('navigate', undefined),
  warmProfile: record('warmProfile', undefined),
  dropLeftoverProfilePrefs: record('dropLeftoverProfilePrefs', undefined),
  deleteProfile: record('deleteProfile', Promise.resolve()),
  activeConnectionId: () => stateAtoms.connectionId.get(),
  connections: record('connections', Promise.resolve([])),
  agents: record('agents', Promise.resolve([])),
  warmAgent: record('warmAgent', undefined),
  ensureAgent: record('ensureAgent', Promise.resolve()),
  sessions: {},
  skills: {},
  toolsets: {},
  profiles: {},
  pluginDecisions: {},
  openSession: record('openSession', Promise.resolve()),
  openWorkspace: openWorkspaceImpl,
  setWorkspaceOwnerLabel: record('setWorkspaceOwnerLabel', undefined),
  setWorkspaceScope: record('setWorkspaceScope', true),
  newChat: record('newChat', undefined),
  focusOpenWorkspaceSession: record('focusOpenWorkspaceSession', false),
  paneVisibility: record('paneVisibility', atom(true)),
  undismissPane: record('undismissPane', undefined),
  revealPane: record('revealPane', undefined),
  onEvent: record('onEvent', () => {}),
  restartGateway: record('restartGateway', Promise.resolve()),
  status: record('status', Promise.resolve({})),
  profileRoutes: record('profileRoutes', Promise.resolve([])),
  requestProfile: record('requestProfile', Promise.resolve({})),
  retainProfileSocket: record('retainProfileSocket', () => {}),
  retainProfile: record('retainProfile', Promise.resolve(() => {})),
  listPersistedSessions: record('listPersistedSessions', Promise.resolve([])),
  setPersistedSessionHidden: record('setPersistedSessionHidden', Promise.resolve()),
  request: record('request', Promise.resolve({})),
  getGateway: () => null,
  traceIdentityChange: record('traceIdentityChange', undefined),
  composer: {
    getDraft: record('composer.getDraft', Promise.resolve(null)),
    setDraft: record('composer.setDraft', Promise.resolve(true)),
    insertText: record('composer.insertText', Promise.resolve(true)),
    submit: record('composer.submit', true),
    focus: record('composer.focus', undefined)
  },
  spellcheck: {
    addWord: record('spellcheck.addWord', Promise.resolve(true)),
    removeWord: record('spellcheck.removeWord', Promise.resolve(true))
  },
  i18n: {
    registerAppLocale: record('i18n.registerAppLocale', () => {}),
    languageOptions: () => []
  }
}

/** Calls of one host method (or all when omitted). */
export function callsOf(method) {
  return method ? hostCalls.filter(call => call.method === method) : [...hostCalls]
}

/** Text of every toast shown through host.notify (string or { message } inputs). */
export function notifyMessages() {
  return callsOf('notify').map(({ args: [input] }) => (typeof input === 'string' ? input : input?.message))
}

/** Everything shown through host.notify as one lowercase-insensitive searchable string each. */
export function toastTexts() {
  return callsOf('notify').map(({ args: [input] }) => (typeof input === 'string' ? input : JSON.stringify(input)))
}

/** Simulate an older desktop that does not expose host.openWorkspace. */
export function removeOpenWorkspace() {
  delete host.openWorkspace
}

export function restoreOpenWorkspace() {
  host.openWorkspace = openWorkspaceImpl
}

export function resetHost() {
  hostCalls.length = 0
  restoreOpenWorkspace()
  stateAtoms.connectionId.set('local')
  stateAtoms.profile.set('default')
  stateAtoms.focusedSessionId.set(null)
}
