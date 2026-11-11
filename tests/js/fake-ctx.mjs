// Fake PluginContext (apps/desktop/src/contrib/plugin.ts, Hermes v0.23.0).
//
//   const ctx = createFakeCtx({ overrides: { 'GET /health': { status: 404 } } })
//   plugin.register(ctx)
//   ctx.fake.registrations / .restCalls / .errors / .emit(type, payload) / ...
//
// - register/registerMany/registerSettingsPage namespace ids as `<pluginId>:<id>`
//   and stamp `source`, exactly like the real host.
// - rest() resolves JSON fixtures through tests/fixtures/desktop/routes.json and
//   records every call. A call that matches no fixture is recorded in
//   ctx.fake.errors (the smoke run fails) and rejects with a 404.
// - Rejections mirror the real transport: a plain Error whose message starts
//   with "<status>: <body>" (a 404 is exactly that; other statuses carry
//   Electron's "Error invoking remote method 'hermes:api': Error: " prefix). The
//   real error has NO status field once it crossed the IPC bridge, so callers
//   must classify by message.
// - Timers are never real: setInterval/setTimeout only record.
import { readFileSync } from 'node:fs'

const FIXTURE_DIR = new URL('../fixtures/desktop/', import.meta.url)

let routesCache = null

export function loadRoutes() {
  if (!routesCache) {
    routesCache = JSON.parse(readFileSync(new URL('routes.json', FIXTURE_DIR), 'utf8')).routes
  }

  return routesCache
}

export function readFixture(name) {
  return JSON.parse(readFileSync(new URL(name, FIXTURE_DIR), 'utf8'))
}

const clone = value => JSON.parse(JSON.stringify(value))

function templateRegExp(template) {
  const source = template
    .split('/')
    .map(part => (/^\{[^}]+\}$/.test(part) ? '[^/]+' : part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
    .join('/')

  return new RegExp(`^${source}$`)
}

const paramCount = template => (template.match(/\{[^}]+\}/g) ?? []).length

export function parsePath(path) {
  const suffix = path.startsWith('/') ? path : `/${path}`
  const mark = suffix.indexOf('?')
  const pathname = (mark < 0 ? suffix : suffix.slice(0, mark)).split('#')[0]
  const queryString = mark < 0 ? '' : suffix.slice(mark + 1).split('#')[0]
  const query = {}

  for (const [key, value] of new URLSearchParams(queryString)) {
    query[key] = value
  }

  return { pathname, query }
}

/**
 * Find the route for a request: method + path template (fewest params wins),
 * then the most specific `query` variant whose keys all match.
 */
export function matchRoute(method, path, routes = loadRoutes()) {
  const { pathname, query } = parsePath(path)
  const candidates = routes.filter(route => route.method === method && templateRegExp(route.path).test(pathname))

  if (candidates.length === 0) {
    return null
  }

  const best = Math.min(...candidates.map(route => paramCount(route.path)))
  const sameShape = candidates.filter(route => paramCount(route.path) === best)
  const ok = sameShape.filter(route => Object.entries(route.query ?? {}).every(([key, value]) => query[key] === String(value)))

  ok.sort((a, b) => Object.keys(b.query ?? {}).length - Object.keys(a.query ?? {}).length)

  return ok[0] ?? null
}

export function restError(status, body) {
  const text = JSON.stringify(body ?? { error: { code: 'error', message: `HTTP ${status}` } })
  const message = status === 404 ? `404: ${text}` : `Error invoking remote method 'hermes:api': Error: ${status}: ${text}`

  return new Error(message)
}

function overrideFor(overrides, method, path, route) {
  const { pathname } = parsePath(path)
  const keys = [`${method} ${path}`, `${method} ${pathname}`]

  if (route) {
    keys.push(`${method} ${route.path}`)
  }

  for (const key of keys) {
    if (Object.hasOwn(overrides, key)) {
      return overrides[key]
    }
  }

  return undefined
}

export function createFakeCtx({ overrides = {}, pluginId = 'cloud-compute' } = {}) {
  const fake = {
    pluginId,
    registrations: [],
    registrationDisposed: [],
    disposers: [],
    listeners: new Map(),
    intervals: [],
    timeouts: [],
    domListeners: [],
    restCalls: [],
    errors: [],
    storageData: new Map(),
    osCalls: { notify: [], openExternal: [], revealPath: [], writeClipboard: [], pickSavePath: [], pickOpenPath: [] },
    overrides
  }

  const source = `plugin:${pluginId}`

  const track = dispose => {
    fake.disposers.push(dispose)

    return dispose
  }

  const addRegistration = contribution => {
    if (!contribution || typeof contribution.id !== 'string' || !contribution.id) {
      fake.errors.push(`register: contribution without a string id (${JSON.stringify(contribution?.area)})`)
    }

    if (!contribution || typeof contribution.area !== 'string' || !contribution.area) {
      fake.errors.push(`register: contribution "${contribution?.id}" has no area`)
    }

    const scoped = { ...contribution, id: `${pluginId}:${contribution.id}`, source }

    if (fake.registrations.some(r => r.area === scoped.area && r.id === scoped.id)) {
      fake.errors.push(`register: duplicate contribution ${scoped.area}/${scoped.id}`)
    }

    fake.registrations.push(scoped)

    return () => {
      const index = fake.registrations.indexOf(scoped)

      if (index >= 0) {
        fake.registrations.splice(index, 1)
        fake.registrationDisposed.push(scoped)
      }
    }
  }

  const ctx = {
    source,
    register: c => track(addRegistration(c)),
    registerMany: cs => {
      const disposers = cs.map(addRegistration)

      return track(() => disposers.forEach(dispose => dispose()))
    },
    registerSettingsPage: page =>
      track(
        addRegistration({
          area: 'settings.plugins',
          data: { children: page.children ?? [], icon: page.icon },
          id: page.id,
          order: page.order,
          render: page.render,
          title: page.title
        })
      ),
    onDispose: fn => void track(fn),
    onEvent: (type, listener) => {
      const set = fake.listeners.get(type) ?? new Set()

      set.add(listener)
      fake.listeners.set(type, set)

      return track(() => {
        set.delete(listener)
      })
    },
    setTimeout: (fn, ms) => {
      const entry = { fn, ms, cancelled: false }

      fake.timeouts.push(entry)

      return track(() => {
        entry.cancelled = true
      })
    },
    setInterval: (fn, ms) => {
      const entry = { fn, ms, cancelled: false }

      fake.intervals.push(entry)

      return track(() => {
        entry.cancelled = true
      })
    },
    addEventListener: (target, type, listener, options) => {
      fake.domListeners.push({ target, type, listener, options })

      return track(() => {})
    },
    rest: (path, opts = {}) => restImpl(path, opts),
    socket: () => track(() => {}),
    os: {
      notify: input => void fake.osCalls.notify.push(input),
      openExternal: async url => {
        fake.osCalls.openExternal.push(url)

        return true
      },
      revealPath: async path => {
        fake.osCalls.revealPath.push(path)

        return true
      },
      pickSavePath: async options => {
        fake.osCalls.pickSavePath.push(options)

        return null
      },
      pickOpenPath: async options => {
        fake.osCalls.pickOpenPath.push(options)

        return null
      },
      writeClipboard: async text => {
        fake.osCalls.writeClipboard.push(text)

        return true
      }
    },
    pet: { say: () => () => {}, clear: () => {}, visible: { get: () => false, listen: () => () => {}, subscribe: () => () => {} } },
    runAction: id => ({ ok: true, id }),
    listActions: () => [],
    preview: { executeJavaScript: async () => ({ ok: false }) },
    storage: {
      get: (key, fallback) => (fake.storageData.has(key) ? clone(fake.storageData.get(key)) : fallback),
      set: (key, value) => void fake.storageData.set(key, clone(value)),
      remove: key => void fake.storageData.delete(key)
    },
    files: {},
    i18n: { register: () => () => {}, t: key => String(key) }
  }

  async function restImpl(path, opts) {
    const method = String(opts?.method ?? 'GET').toUpperCase()

    if (typeof path !== 'string') {
      throw new Error(`ctx.rest: path must be a string, got ${typeof path}`)
    }

    if (parsePath(path).pathname.split('/').includes('..')) {
      throw new Error(`pluginRest: illegal path traversal in "${path}"`)
    }

    const { pathname, query } = parsePath(path)
    const route = matchRoute(method, path)
    const call = { method, path, pathname, query, body: opts?.body, route: route ? { method: route.method, path: route.path, query: route.query } : null }

    fake.restCalls.push(call)

    const override = overrideFor(overrides, method, path, route)

    if (override !== undefined) {
      if (typeof override === 'function') {
        return clone(await override(call))
      }

      if (override.status !== undefined && override.status >= 400) {
        call.rejected = override.status

        throw restError(override.status, override.body ?? { error: { code: override.status === 404 ? 'not_found' : 'error', message: `HTTP ${override.status}` } })
      }

      if (override.fixture) {
        return readFixture(override.fixture)
      }

      if (Object.hasOwn(override, 'body')) {
        return clone(override.body)
      }
    }

    if (!route) {
      const message = `no fixture for ${method} ${path}`

      fake.errors.push(message)

      throw restError(404, { error: { code: 'not_found', message } })
    }

    call.fixture = route.fixture

    return readFixture(route.fixture)
  }

  fake.emit = (type, payload = {}) => {
    const event = { type, payload }

    for (const key of [type, '*']) {
      for (const listener of [...(fake.listeners.get(key) ?? [])]) {
        listener(event)
      }
    }
  }

  /** Run every recorded, non-cancelled interval callback once. */
  fake.tickIntervals = async () => {
    for (const entry of fake.intervals) {
      if (!entry.cancelled) {
        await entry.fn()
      }
    }
  }

  fake.byArea = area => fake.registrations.filter(r => r.area === area)
  fake.find = (area, localId) => fake.registrations.find(r => r.area === area && r.id === `${pluginId}:${localId}`)
  fake.callsTo = (method, template) => fake.restCalls.filter(c => c.method === method && (c.route?.path === template || c.pathname === template))
  fake.dispose = () => {
    for (const dispose of [...fake.disposers].reverse()) {
      dispose()
    }
  }

  Object.defineProperty(ctx, 'fake', { value: fake, enumerable: false })

  return ctx
}
