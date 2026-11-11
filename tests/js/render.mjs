// Shallow renderer for function components, used by the desktop test harness.
//
//   const tree = await renderUntilStable(jsx(Page, {}))
//   findButtons(tree).byLabel('Refresh')
//   const next = await click(findButtons(tree).byLabel('Refresh'))
//
// Components are called recursively; hook state is kept per component path
// across re-renders. useQuery/useMutation are backed by a per-session cache and
// settle through renderUntilStable(). No real timers are used.
import { Fragment } from './stubs/jsx-runtime.mjs'

const tick = () => new Promise(resolve => setImmediate(resolve))

let currentSession = null
let currentFiber = null
let latestSession = null

export { Fragment }

const isElement = value => value !== null && typeof value === 'object' && value.$$typeof === 'element'

function describeNode(node) {
  const base = typeof node?.type === 'string' ? `<${node.type}>` : `<${node?.name ?? '?'}>`
  const label = node ? textOf(node) : ''

  return label ? `${base} "${label.slice(0, 60)}"` : base
}

function wrapRenderError(error, name, path) {
  if (error && error.ccRenderError) {
    return error
  }

  const message = error instanceof Error ? error.message : String(error)
  const wrapped = new Error(`Render error in <${name}> (${path}): ${message}`, { cause: error })

  Object.defineProperty(wrapped, 'ccRenderError', { value: true })

  if (error instanceof Error && error.stack) {
    wrapped.stack = `${wrapped.message}\n--- cause ---\n${error.stack}`
  }

  return wrapped
}

// ---------------------------------------------------------------- session

class Session {
  constructor(element, { maxPasses = 30 } = {}) {
    this.element = element
    this.maxPasses = maxPasses
    this.fibers = new Map()
    this.cache = new Map()
    this.pending = new Set()
    this.dirty = false
    this.contextStack = []
    this.tree = null
    this.renders = 0
    latestSession = this
  }

  track(promise) {
    const tracked = promise.finally(() => {
      this.pending.delete(tracked)
      this.dirty = true
    })

    tracked.catch(() => {})
    this.pending.add(tracked)

    return tracked
  }

  render() {
    const previousSession = currentSession
    const previousFiber = currentFiber

    this.seen = new Set()
    this.effectQueue = []
    this.dirty = false
    this.renders += 1
    currentSession = this
    currentFiber = null

    let tree

    try {
      const children = renderChildren(this, this.element, 'root', [])

      tree = makeNode('#root', '#root', {}, children, 'root', this)
    } finally {
      currentSession = previousSession
      currentFiber = previousFiber
    }

    // Unmount components that were not rendered in this pass.
    for (const [path, fiber] of [...this.fibers]) {
      if (!this.seen.has(path)) {
        runCleanups(fiber)
        this.fibers.delete(path)
      }
    }

    // Run effects (after commit). They may call setState -> dirty.
    currentSession = this

    try {
      for (const { hook, fn } of this.effectQueue) {
        if (typeof hook.cleanup === 'function') {
          hook.cleanup()
          hook.cleanup = undefined
        }

        const cleanup = fn()

        hook.cleanup = typeof cleanup === 'function' ? cleanup : undefined
      }
    } finally {
      currentSession = previousSession
    }

    this.tree = tree

    return tree
  }

  async settle(maxPasses = this.maxPasses) {
    for (let pass = 0; pass < maxPasses; pass += 1) {
      this.render()

      if (this.pending.size > 0) {
        await Promise.allSettled([...this.pending])

        continue
      }

      if (this.dirty) {
        continue
      }

      // One macrotask turn catches promise chains started by effects/handlers.
      await tick()

      if (this.pending.size === 0 && !this.dirty) {
        return this.tree
      }
    }

    throw new Error(
      `renderUntilStable: not stable after ${maxPasses} passes ` +
        `(pending queries: ${[...this.cache.values()].filter(e => e.promise).map(e => e.hash).join(', ') || 'none'}, ` +
        `dirty: ${this.dirty})`
    )
  }

  unmount() {
    for (const fiber of this.fibers.values()) {
      runCleanups(fiber)
    }

    this.fibers.clear()
  }
}

function runCleanups(fiber) {
  for (const hook of fiber.hooks) {
    if (hook.kind === 'effect' && typeof hook.cleanup === 'function') {
      try {
        hook.cleanup()
      } finally {
        hook.cleanup = undefined
      }
    }
  }
}

function makeNode(type, name, props, children, path, session) {
  const node = { type, name, props, children, path }

  Object.defineProperty(node, 'session', { value: session, enumerable: false })

  return node
}

// ---------------------------------------------------------------- rendering

function renderChildren(session, children, path, out) {
  if (children === null || children === undefined || typeof children === 'boolean') {
    return out
  }

  if (Array.isArray(children)) {
    children.forEach((child, index) => {
      const key = isElement(child) && child.key !== null ? `#${child.key}` : `[${index}]`

      renderChildren(session, child, `${path}${key}`, out)
    })

    return out
  }

  if (typeof children === 'string' || typeof children === 'number' || typeof children === 'bigint') {
    out.push(String(children))

    return out
  }

  if (isElement(children)) {
    renderElement(session, children, path, out)

    return out
  }

  throw new Error(`Objects are not valid as a React child (at ${path}): ${Object.prototype.toString.call(children)}`)
}

function renderElement(session, element, path, out) {
  const { type, props } = element

  if (type === Fragment) {
    renderChildren(session, props.children, path, out)

    return
  }

  if (typeof type === 'string') {
    const children = renderChildren(session, props.children, `${path}/${type}`, [])

    out.push(makeNode(type, type, props, children, `${path}/${type}`, session))

    return
  }

  if (type && typeof type === 'object' && type.$$typeof === 'provider') {
    session.contextStack.push([type.context, props.value])

    try {
      renderChildren(session, props.children, path, out)
    } finally {
      session.contextStack.pop()
    }

    return
  }

  if (typeof type === 'function') {
    if (type.prototype && type.prototype.isReactComponent) {
      throw new Error(`Class components are not supported by the test renderer: ${type.name}`)
    }

    const name = type.displayName || type.name || 'Anonymous'
    const cpath = `${path}/${name}`
    let fiber = session.fibers.get(cpath)

    if (!fiber || fiber.type !== type) {
      if (fiber) {
        runCleanups(fiber)
      }

      fiber = { path: cpath, type, name, hooks: [], index: 0 }
      session.fibers.set(cpath, fiber)
    }

    session.seen.add(cpath)
    fiber.index = 0

    const previousFiber = currentFiber
    let result

    currentFiber = fiber

    try {
      result = type(props)
    } catch (error) {
      throw wrapRenderError(error, name, cpath)
    } finally {
      currentFiber = previousFiber
    }

    const children = renderChildren(session, result, cpath, [])

    out.push(makeNode(type, name, props, children, cpath, session))

    return
  }

  throw new Error(`Unsupported element type at ${path}: ${String(type)}`)
}

// ---------------------------------------------------------------- hooks

function nextHook(kind, init) {
  const fiber = currentFiber

  if (!fiber) {
    throw new Error(`Hook "${kind}" called outside a component render`)
  }

  const index = fiber.index
  fiber.index += 1

  if (index >= fiber.hooks.length) {
    fiber.hooks.push({ kind, ...init() })
  }

  const hook = fiber.hooks[index]

  if (hook.kind !== kind) {
    throw new Error(`Hook order changed in <${fiber.name}> (hook #${index}: was ${hook.kind}, now ${kind})`)
  }

  return hook
}

function depsChanged(previous, next) {
  if (!next || !previous) {
    return true
  }

  if (previous.length !== next.length) {
    return true
  }

  return next.some((value, index) => !Object.is(value, previous[index]))
}

export function useState(initial) {
  const session = currentSession
  const hook = nextHook('state', () => ({
    value: typeof initial === 'function' ? initial() : initial,
    set: null
  }))

  if (!hook.set) {
    hook.set = update => {
      const next = typeof update === 'function' ? update(hook.value) : update

      if (!Object.is(next, hook.value)) {
        hook.value = next
        session.dirty = true
      }
    }
  }

  return [hook.value, hook.set]
}

export function useReducer(reducer, initialArg, init) {
  const session = currentSession
  const hook = nextHook('reducer', () => ({
    value: init ? init(initialArg) : initialArg,
    dispatch: null,
    reducer
  }))

  hook.reducer = reducer

  if (!hook.dispatch) {
    hook.dispatch = action => {
      const next = hook.reducer(hook.value, action)

      if (!Object.is(next, hook.value)) {
        hook.value = next
        session.dirty = true
      }
    }
  }

  return [hook.value, hook.dispatch]
}

export function useRef(initial) {
  return nextHook('ref', () => ({ ref: { current: initial } })).ref
}

export function useMemo(factory, deps) {
  const hook = nextHook('memo', () => ({ deps: undefined, value: undefined, ready: false }))

  if (!hook.ready || depsChanged(hook.deps, deps)) {
    hook.value = factory()
    hook.deps = deps
    hook.ready = true
  }

  return hook.value
}

export function useCallback(fn, deps) {
  return useMemo(() => fn, deps)
}

function effectHook(fn, deps) {
  const session = currentSession
  const hook = nextHook('effect', () => ({ deps: undefined, cleanup: undefined, ran: false }))

  if (!hook.ran || depsChanged(hook.deps, deps)) {
    hook.ran = true
    hook.deps = deps
    session.effectQueue.push({ hook, fn })
  }
}

export const useEffect = effectHook
export const useLayoutEffect = effectHook
export const useInsertionEffect = effectHook

export function createContext(defaultValue) {
  const context = { $$typeof: 'context', _default: defaultValue }

  context.Provider = { $$typeof: 'provider', context }
  context.Consumer = ({ children }) => (typeof children === 'function' ? children(defaultValue) : null)

  return context
}

export function useContext(context) {
  const session = currentSession

  if (!session) {
    throw new Error('useContext called outside a render')
  }

  nextHook('context', () => ({}))

  for (let index = session.contextStack.length - 1; index >= 0; index -= 1) {
    if (session.contextStack[index][0] === context) {
      return session.contextStack[index][1]
    }
  }

  return context._default
}

export function useId() {
  const fiber = currentFiber
  const hook = nextHook('id', () => ({ id: `:r${fiber.path.length}${fiber.hooks.length}:` }))

  return hook.id
}

export function useSyncExternalStore(_subscribe, getSnapshot) {
  nextHook('sync', () => ({}))

  return getSnapshot()
}

export function useDeferredValue(value) {
  nextHook('deferred', () => ({}))

  return value
}

export function useTransition() {
  nextHook('transition', () => ({}))

  return [false, fn => fn()]
}

export function useImperativeHandle() {
  nextHook('imperative', () => ({}))
}

export function useDebugValue() {}

export function startTransition(fn) {
  fn()
}

// ---------------------------------------------------------------- query layer

const matchesKey = (prefix, key) =>
  Array.isArray(prefix) && prefix.length <= key.length && prefix.every((part, index) => JSON.stringify(part) === JSON.stringify(key[index]))

function startFetch(session, entry) {
  if (entry.promise) {
    return entry.promise
  }

  entry.fetches += 1

  const run = Promise.resolve()
    .then(() => entry.queryFn({ queryKey: entry.queryKey, signal: undefined }))
    .then(
      data => {
        entry.data = data
        entry.status = 'success'
        entry.error = null
      },
      error => {
        entry.error = error
        entry.status = 'error'
      }
    )
    .finally(() => {
      entry.promise = null
      entry.stale = false
    })

  entry.promise = session.track(run)

  return entry.promise
}

export function useQuery(options) {
  const session = currentSession

  if (!options || typeof options !== 'object' || Array.isArray(options)) {
    throw new Error('useQuery: expected the object form useQuery({ queryKey, queryFn, ... })')
  }

  const { queryKey, queryFn, enabled = true, refetchInterval, select } = options

  if (!Array.isArray(queryKey)) {
    throw new Error('useQuery: queryKey must be an array')
  }

  if (typeof queryFn !== 'function') {
    throw new Error('useQuery: queryFn must be a function')
  }

  nextHook('query', () => ({}))

  const hash = JSON.stringify(queryKey)
  let entry = session.cache.get(hash)

  if (!entry) {
    entry = {
      hash,
      queryKey,
      queryFn,
      status: 'pending',
      data: undefined,
      error: null,
      stale: false,
      promise: null,
      fetches: 0,
      refetchInterval: undefined,
      enabled: true
    }
    session.cache.set(hash, entry)
  }

  entry.queryFn = queryFn
  entry.refetchInterval = refetchInterval
  entry.enabled = enabled !== false

  if (entry.enabled && !entry.promise && (entry.status === 'pending' || entry.stale)) {
    startFetch(session, entry)
  }

  const fetching = Boolean(entry.promise)
  const data = entry.status === 'success' && typeof select === 'function' ? select(entry.data) : entry.data

  return {
    data,
    error: entry.error,
    status: entry.status,
    isPending: entry.status === 'pending',
    isLoading: entry.status === 'pending' && fetching,
    isFetching: fetching,
    isRefetching: fetching && entry.status !== 'pending',
    isError: entry.status === 'error',
    isSuccess: entry.status === 'success',
    fetchStatus: fetching ? 'fetching' : 'idle',
    refetch: () => {
      entry.stale = true

      return startFetch(session, entry).then(() => ({ data: entry.data, error: entry.error }))
    }
  }
}

export function useMutation(options) {
  const session = currentSession

  if (!options || typeof options !== 'object' || typeof options.mutationFn !== 'function') {
    throw new Error('useMutation: expected useMutation({ mutationFn, ... })')
  }

  const hook = nextHook('mutation', () => ({
    status: 'idle',
    data: undefined,
    error: null,
    variables: undefined,
    api: null,
    options
  }))

  hook.options = options

  if (!hook.api) {
    const run = (variables, callOptions = {}) => {
      const opts = hook.options

      hook.status = 'pending'
      hook.variables = variables
      hook.error = null
      session.dirty = true

      const promise = (async () => {
        let context

        try {
          context = await opts.onMutate?.(variables)

          const data = await opts.mutationFn(variables)

          hook.data = data
          hook.status = 'success'
          await opts.onSuccess?.(data, variables, context)
          callOptions.onSuccess?.(data, variables, context)
          await opts.onSettled?.(data, null, variables, context)

          return data
        } catch (error) {
          hook.error = error
          hook.status = 'error'
          await opts.onError?.(error, variables, context)
          callOptions.onError?.(error, variables, context)
          await opts.onSettled?.(undefined, error, variables, context)

          throw error
        }
      })()

      session.track(promise)

      return promise
    }

    hook.api = {
      mutateAsync: run,
      mutate: (variables, callOptions) => {
        run(variables, callOptions).catch(() => {})
      },
      reset: () => {
        hook.status = 'idle'
        hook.data = undefined
        hook.error = null
        session.dirty = true
      }
    }
  }

  return {
    ...hook.api,
    status: hook.status,
    data: hook.data,
    error: hook.error,
    variables: hook.variables,
    isIdle: hook.status === 'idle',
    isPending: hook.status === 'pending',
    isError: hook.status === 'error',
    isSuccess: hook.status === 'success'
  }
}

function filtersOf(method, filters) {
  if (Array.isArray(filters)) {
    throw new TypeError(
      `queryClient.${method}: @tanstack/react-query v5 takes a filters object ({ queryKey }), not an array`
    )
  }

  return filters ?? {}
}

export const queryClient = {
  calls: [],
  invalidateQueries(filters) {
    const f = filtersOf('invalidateQueries', filters)

    this.calls.push({ method: 'invalidateQueries', filters: f })

    if (latestSession) {
      for (const entry of latestSession.cache.values()) {
        if (!f.queryKey || matchesKey(f.queryKey, entry.queryKey)) {
          entry.stale = true
        }
      }

      latestSession.dirty = true
    }

    return Promise.resolve()
  },
  refetchQueries(filters) {
    this.calls.push({ method: 'refetchQueries', filters: filtersOf('refetchQueries', filters) })

    return this.invalidateQueries(filters)
  },
  setQueryData(queryKey, data) {
    this.calls.push({ method: 'setQueryData', filters: { queryKey } })

    const entry = latestSession?.cache.get(JSON.stringify(queryKey))

    if (entry) {
      entry.data = typeof data === 'function' ? data(entry.data) : data
      entry.status = 'success'
      latestSession.dirty = true
    }
  },
  getQueryData(queryKey) {
    return latestSession?.cache.get(JSON.stringify(queryKey))?.data
  },
  removeQueries(filters) {
    const f = filtersOf('removeQueries', filters)

    this.calls.push({ method: 'removeQueries', filters: f })

    if (latestSession) {
      for (const [hash, entry] of [...latestSession.cache]) {
        if ((!f.queryKey || matchesKey(f.queryKey, entry.queryKey)) && (!f.predicate || f.predicate(entry))) {
          latestSession.cache.delete(hash)
        }
      }

      latestSession.dirty = true
    }
  },
  cancelQueries() {
    return Promise.resolve()
  },
  clear() {
    latestSession?.cache.clear()
  },
  reset() {
    this.calls.length = 0
  }
}

export function useQueryClient() {
  return queryClient
}

/** Snapshot of the queries the latest session knows about (for assertions). */
export function getQueries(session = latestSession) {
  return [...(session?.cache.values() ?? [])].map(entry => ({
    queryKey: entry.queryKey,
    status: entry.status,
    refetchInterval: entry.refetchInterval,
    enabled: entry.enabled,
    fetches: entry.fetches,
    stale: entry.stale
  }))
}

// ---------------------------------------------------------------- tree helpers

export async function renderUntilStable(element, { maxPasses = 30, session } = {}) {
  const active = session ?? new Session(element, { maxPasses })

  return active.settle(maxPasses)
}

/** Render once (no waiting for queries). Returns the tree. */
export function renderOnce(element) {
  return new Session(element).render()
}

export function currentSessionOf(tree) {
  return tree.session
}

export function findAll(tree, predicate) {
  const test =
    typeof predicate === 'function'
      ? predicate
      : node => node.type === predicate || node.name === predicate
  const found = []

  const visit = node => {
    if (node === null || typeof node !== 'object') {
      return
    }

    if (test(node)) {
      found.push(node)
    }

    for (const child of node.children ?? []) {
      visit(child)
    }
  }

  visit(tree)

  return found
}

export function findFirst(tree, predicate) {
  return findAll(tree, predicate)[0]
}

function collectText(node, parts) {
  if (node === null || node === undefined) {
    return
  }

  if (typeof node === 'string') {
    parts.push(node)

    return
  }

  parts.push(' ')

  for (const child of node.children ?? []) {
    collectText(child, parts)
  }

  parts.push(' ')
}

/** Visible text under a node; element boundaries become spaces, whitespace collapsed. */
export function textOf(node) {
  const parts = []

  collectText(node, parts)

  return parts.join('').replace(/\s+/g, ' ').trim()
}

const BUTTON_NAMES = new Set(['Button', 'RowButton', 'DropdownMenuItem', 'ContextMenuItem', 'PanelAction', 'PanelAddButton'])

export function isButtonNode(node) {
  return (
    node !== null &&
    typeof node === 'object' &&
    (node.type === 'button' || BUTTON_NAMES.has(node.name)) &&
    typeof node.props?.onClick === 'function'
  )
}

/** Buttons under tree, in document order. The array also has byLabel()/allByLabel(). */
export function findButtons(tree) {
  const buttons = findAll(tree, isButtonNode)

  for (const node of buttons) {
    const label = textOf(node) || node.props['aria-label'] || node.props.title || ''

    Object.defineProperty(node, 'label', { value: label, enumerable: false, configurable: true })
  }

  Object.defineProperty(buttons, 'byLabel', {
    value: label => buttons.find(b => b.label === label),
    enumerable: false
  })
  Object.defineProperty(buttons, 'allByLabel', {
    value: label => buttons.filter(b => b.label === label),
    enumerable: false
  })
  Object.defineProperty(buttons, 'labels', {
    value: () => buttons.map(b => b.label),
    enumerable: false
  })

  return buttons
}

/** Invoke a button's onClick, wait for the page to settle, return the new tree. */
export async function click(node, event = {}) {
  if (!node || typeof node.props?.onClick !== 'function') {
    throw new Error(`click: ${describeNode(node)} has no onClick handler`)
  }

  if (node.props.disabled) {
    throw new Error(`click: ${describeNode(node)} is disabled`)
  }

  const synthetic = { preventDefault() {}, stopPropagation() {}, target: node, currentTarget: node, ...event }
  const result = node.props.onClick(synthetic)

  if (result && typeof result.then === 'function') {
    await result
  }

  return node.session.settle()
}

/** Drive an input-like node: onChange({target:{value}}) / onValueChange / onCheckedChange. */
export async function setValue(node, value) {
  const { props } = node
  const target = { value, checked: Boolean(value) }

  if (typeof props.onChange === 'function') {
    props.onChange({ target, currentTarget: target })
  } else if (typeof props.onValueChange === 'function') {
    props.onValueChange(value)
  } else if (typeof props.onCheckedChange === 'function') {
    props.onCheckedChange(Boolean(value))
  } else {
    throw new Error(`setValue: ${describeNode(node)} has no change handler`)
  }

  return node.session.settle()
}

/** Select a tab in the first <Tabs> that has onValueChange. */
export async function selectTab(tree, value) {
  const tabs = findAll(tree, n => n.name === 'Tabs' && typeof n.props.onValueChange === 'function')[0]

  if (!tabs) {
    throw new Error('selectTab: no Tabs with onValueChange in the tree')
  }

  tabs.props.onValueChange(value)

  return tabs.session.settle()
}

export { describeNode }
