// Smoke test for desktop/plugin.js (stubbed SDK, shallow renderer, fixtures).
//   node --import ./tests/js/register-stubs.mjs ./tests/js/smoke.mjs
// Prints "SMOKE OK" and exits 0, or the failed checks and exits 1.
import assert from 'node:assert/strict'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

// ---- timers: plugin code must use ctx.setInterval/ctx.setTimeout ----------
const timerCalls = []
const recorder = kind => (...args) => {
  timerCalls.push({ kind, ms: args[1], stack: new Error(kind).stack })

  return 0
}

globalThis.setInterval = recorder('setInterval')
globalThis.setTimeout = recorder('setTimeout')

const failures = []
let checks = 0

process.on('unhandledRejection', reason => {
  failures.push(`unhandled rejection: ${reason instanceof Error ? reason.stack : reason}`)
})

const { jsx } = await import('./stubs/jsx-runtime.mjs')
const sdk = await import('./stubs/sdk.mjs')
const render = await import('./render.mjs')
const { createFakeCtx, readFixture } = await import('./fake-ctx.mjs')
const hostFake = await import('./host-fake.mjs')

const { renderUntilStable, findAll, findButtons, textOf, click, setValue, selectTab, getQueries, queryClient } = render
const { host, hostStateAtoms, callsOf, notifyMessages, toastTexts, removeOpenWorkspace, resetHost } = hostFake

const root = path.resolve(import.meta.dirname, '..', '..')
const pluginPath = process.env.CC_PLUGIN_PATH || path.join(root, 'desktop', 'plugin.js')
const pluginUrl = pathToFileURL(pluginPath).href

function note(label, error) {
  const detail = error instanceof Error ? error.stack ?? error.message : String(error)

  failures.push(`${label}\n    ${detail.split('\n').slice(0, 14).join('\n    ')}`)
}

async function check(label, fn) {
  checks += 1

  try {
    await fn()
  } catch (error) {
    note(label, error)
  }
}

let mod

try {
  mod = await import(pluginUrl)
} catch (error) {
  note('import desktop/plugin.js through the stubs', error)
}

if (!mod) {
  finish()
}

const plugin = mod.default
const T = mod.__test__

await check('timers not used during import', () => {
  assert.deepEqual(timerCalls.map(c => c.kind), [], `plugin.js called a global timer while importing:\n${timerCalls[0]?.stack}`)
})

await check('default export: id, name, defaultEnabled', () => {
  assert.equal(plugin?.id, 'cloud-compute')
  assert.equal(typeof plugin.name, 'string')
  assert.equal(plugin.defaultEnabled, false)
  assert.equal(typeof plugin.register, 'function')
})

await check('__test__ API surface', () => {
  assert.ok(T && typeof T === 'object', 'named export __test__ is missing')

  for (const name of ['instanceActions', 'openTerminal', 'openScreen', 'chipModel', 'shouldInvalidate', 'queryKey']) {
    assert.equal(typeof T[name], 'function', `__test__.${name} must be a function`)
  }

  assert.ok(Array.isArray(T.API_CALLS) && T.API_CALLS.length > 0, '__test__.API_CALLS must be a non-empty array')
  assert.ok(T.COPY && typeof T.COPY === 'object', '__test__.COPY must be an object')

  for (const name of ['Page', 'OverviewTab', 'HostsTab', 'TemplatesTab', 'JobsTab', 'AgentsTab', 'AgentRunCard', 'NewAgentDialog', 'TemplateEditor', 'ForwardDialog', 'TunnelRow', 'HelpTab', 'StatusChip', 'SettingsPage', 'InstanceCard', 'HostCard', 'AddHostWizard']) {
    assert.equal(typeof T.components?.[name], 'function', `__test__.components.${name} must be a function`)
  }
})

if (!T || !T.components) {
  finish()
}

const C = T.components

// ---- helpers ----------------------------------------------------------------
async function withPlugin(overrides, fn) {
  resetHost()
  queryClient.reset()
  hostStateAtoms.connectionId.set('local')
  hostStateAtoms.profile.set('default')

  const ctx = createFakeCtx({ overrides })

  plugin.register(ctx)

  try {
    return await fn(ctx, ctx.fake)
  } finally {
    ctx.fake.dispose()
    assert.deepEqual(ctx.fake.errors, [], 'fixture / registration errors')
  }
}

const mountComponent = (Component, props = {}) => renderUntilStable(jsx(Component, props))
const labelsOf = tree => findButtons(tree).labels()
const allText = tree => textOf(tree)

const BUTTON_LABELS = {
  terminal: 'Terminal',
  screen: 'Screen',
  forward: 'Forward port…',
  logs: 'Logs',
  stop: 'Stop',
  start: 'Start',
  destroy: 'Delete',
  files: 'Files\u2026',
  extend: 'Extend\u2026',
  chat: 'Use in chat'
}

const instancesFixture = readFixture('instances.json')
const byId = Object.fromEntries(instancesFixture.map(i => [i.id, i]))
const PLAIN = byId['i-ab12cd34']
const DISPLAY = byId['i-ef56aa01']
const STOPPED = byId['i-0c9d8e7f']

// ---- register(ctx) ----------------------------------------------------------
await check('register(ctx): contributions, ids, events, chip interval', () =>
  withPlugin({}, (ctx, fake) => {
    const all = fake.registrations

    for (const r of all) {
      assert.ok(r.id.startsWith('cloud-compute:'), `contribution id not namespaced: ${r.id}`)
      assert.equal(r.source, 'plugin:cloud-compute')
    }

    const nav = fake.find(sdk.SIDEBAR_NAV_AREA, 'nav')

    assert.ok(nav, 'SIDEBAR_NAV_AREA contribution cloud-compute:nav missing')
    assert.equal(nav.data?.path, '/cloud-compute')
    assert.equal(nav.data?.label, 'Compute')
    assert.equal(nav.data?.codicon, 'server-environment')

    const route = fake.find(sdk.ROUTES_AREA, 'page')

    assert.ok(route, 'ROUTES_AREA contribution cloud-compute:page missing')
    assert.equal(route.data?.path, '/cloud-compute')
    assert.equal(typeof route.render, 'function', 'route needs a render function')

    const chip = fake.find(sdk.STATUSBAR_AREAS.right, 'chip')

    assert.ok(chip, 'STATUSBAR_AREAS.right contribution cloud-compute:chip missing')
    assert.equal(typeof chip.render, 'function')

    const palette = fake.byArea(sdk.PALETTE_AREA)

    assert.deepEqual(
      palette.map(p => p.id).sort(),
      ['cloud-compute:add-host', 'cloud-compute:new-agent', 'cloud-compute:open', 'cloud-compute:spawn', 'cloud-compute:submit-job'],
      'PALETTE_AREA must hold exactly open, add-host, spawn, submit-job, new-agent'
    )

    const labels = Object.fromEntries(palette.map(p => [p.id.split(':')[1], p.data?.label ?? p.title]))

    assert.deepEqual(labels, {
      open: 'Open Compute',
      'add-host': 'Add host',
      spawn: 'Spawn environment…',
      'submit-job': 'Submit Slurm job…',
      'new-agent': 'New cloud agent run…'
    })

    const settings = fake.byArea('settings.plugins')

    assert.equal(settings.length, 1, 'registerSettingsPage must be called once')
    assert.equal(settings[0].id, 'cloud-compute:cloud-compute')
    assert.equal(settings[0].title, 'Own Compute')
    assert.equal(typeof settings[0].render, 'function')
    assert.equal(plugin.name, 'Own Compute', 'the plugin list shows the public name')
    assert.equal(plugin.id, 'cloud-compute', 'the internal id does not change')
    assert.ok(plugin.description.startsWith('Bring your own compute'), 'the description opens with the slogan')

    for (const type of [
      'plugin.cloud-compute.instances.changed',
      'plugin.cloud-compute.host.status',
      'plugin.cloud-compute.job.finished',
      'tool.complete'
    ]) {
      assert.ok(fake.listeners.get(type)?.size > 0, `no onEvent listener for ${type}`)
    }

    assert.ok(fake.intervals.length >= 1, 'chip refresh must use ctx.setInterval')
    assert.ok(fake.intervals.some(i => i.ms === 30000), 'chip refresh interval should be 30000 ms')
    assert.equal(fake.timeouts.length, 0, 'register() should not start timeouts')
  })
)

await check('events invalidate [cloud-compute] queries; notifications on job/host/instance events', () =>
  withPlugin({}, async (ctx, fake) => {
    const invalidations = () => queryClient.calls.filter(c => c.method === 'invalidateQueries').map(c => c.filters?.queryKey)
    const isOurs = key => Array.isArray(key) && key[0] === 'cloud-compute'

    queryClient.reset()
    fake.emit('plugin.cloud-compute.instances.changed', { ids: ['i-ab12cd34'], host_id: 'x1carbon', reason: 'spawned' })
    assert.ok(invalidations().some(isOurs), 'instances.changed must invalidate ["cloud-compute"] queries')

    queryClient.reset()
    fake.emit('tool.complete', { name: 'env_spawn' })
    assert.ok(invalidations().some(isOurs), 'tool.complete env_spawn must invalidate')

    queryClient.reset()
    fake.emit('tool.complete', { name: 'web_search' })
    assert.equal(invalidations().length, 0, 'tool.complete web_search must not invalidate')

    hostFake.hostCalls.length = 0
    fake.emit('plugin.cloud-compute.job.finished', {
      id: 'j-1a2b3c4d', host_id: 'slurm-x1', slurm_job_id: '42', name: 'train-small', state: 'COMPLETED', exit_code: '0:0'
    })
    assert.ok(callsOf('notify').length >= 1, 'job.finished should show a toast through host.notify')

    hostFake.hostCalls.length = 0
    fake.emit('plugin.cloud-compute.host.status', { id: 'lab-gpu', status: 'offline', prev: 'online', last_error: 'timed out' })
    assert.ok(callsOf('notify').length >= 1, 'host.status -> offline should show a toast')

    hostFake.hostCalls.length = 0
    fake.emit('plugin.cloud-compute.instances.changed', { ids: ['i-ab12cd34'], host_id: 'x1carbon', reason: 'error' })
    assert.ok(callsOf('notify').length >= 1, 'instances.changed reason=error should show a toast')

    // the chip refresh callback runs without touching global timers
    await fake.tickIntervals()
  })
)

// ---- pure functions -----------------------------------------------------------
await check('chipModel', () => {
  const sum = (hosts, running = 0, jobs = 0, attention = []) => ({
    hosts: { total: 0, online: 0, degraded: 0, auth_required: 0, offline: 0, ...hosts },
    instances: { running, total: running },
    jobs: { active: jobs, finished_24h: 0 },
    attention,
    generated_at: '2026-11-08T14:30:00Z'
  })
  const dot = ' \u00b7 '

  assert.deepEqual(T.chipModel(sum({ total: 3, online: 3 }, 3, 1)), { text: `3/3 hosts${dot}3 envs${dot}1 job`, tone: 'ok' })
  assert.deepEqual(T.chipModel(sum({ total: 3, online: 2, degraded: 1 }, 3, 1)), { text: `2/3 hosts${dot}3 envs${dot}1 job`, tone: 'warn' })
  assert.equal(T.chipModel(sum({ total: 3, online: 2, auth_required: 1 }, 0, 0)).tone, 'warn')
  assert.equal(T.chipModel(sum({ total: 3, online: 2, offline: 1 }, 0, 0)).tone, 'error')
  assert.equal(T.chipModel(sum({ total: 4, online: 1, offline: 1, degraded: 1, auth_required: 1 })).tone, 'error', 'offline beats degraded')
  assert.equal(T.chipModel(sum({ total: 2, online: 2 }, 0, 0, [{ kind: 'x', id: 'y', message: 'z' }])).tone, 'warn', 'attention -> warn')
  assert.deepEqual(T.chipModel(sum({ total: 0 })), { text: 'No hosts', tone: 'idle' })
  assert.deepEqual(T.chipModel(sum({ total: 1, online: 1 }, 1, 1)), { text: `1/1 host${dot}1 env${dot}1 job`, tone: 'ok' })
  assert.deepEqual(T.chipModel(sum({ total: 2, online: 2 }, 0, 0)), { text: `2/2 hosts${dot}0 envs${dot}0 jobs`, tone: 'ok' })
  assert.deepEqual(T.chipModel(readFixture('summary.json')).tone, 'error', 'fixture summary has an offline host')
})

await check('shouldInvalidate', () => {
  const yes = [
    ['plugin.cloud-compute.x', {}],
    ['plugin.cloud-compute.instances.changed', { ids: [] }],
    ['tool.complete', { name: 'env_spawn' }],
    ['tool.complete', { name: 'slurm_jobs' }],
    ['tool.complete', { name: 'compute_status' }],
    ['tool.complete', { name: 'cloud_agent_spawn' }]
  ]
  const no = [
    ['tool.complete', { name: 'web_search' }],
    ['tool.complete', {}],
    ['tool.start', { name: 'env_spawn' }],
    ['message.delta', { text: 'x' }],
    ['plugin.other.thing', {}]
  ]

  for (const [type, payload] of yes) {
    assert.equal(T.shouldInvalidate(type, payload), true, `${type} ${JSON.stringify(payload)} should invalidate`)
  }

  for (const [type, payload] of no) {
    assert.equal(T.shouldInvalidate(type, payload), false, `${type} ${JSON.stringify(payload)} should not invalidate`)
  }
})

await check('queryKey follows connectionId + profile', () => {
  hostStateAtoms.connectionId.set('conn-a')
  hostStateAtoms.profile.set('prof-b')

  try {
    assert.deepEqual(T.queryKey('hosts'), ['cloud-compute', 'conn-a', 'prof-b', 'hosts'])
    assert.deepEqual(T.queryKey(), ['cloud-compute', 'conn-a', 'prof-b'])
  } finally {
    hostStateAtoms.connectionId.set('local')
    hostStateAtoms.profile.set('default')
  }
})

await check('instanceActions rules', () => {
  const allowed = new Set(Object.keys(BUTTON_LABELS))
  const make = (status, display_state, display) => ({ id: 'i-1', status, display_state, display })
  const cases = [
    [make('running', 'supported', true), { has: ['terminal', 'screen', 'forward', 'logs', 'files', 'stop', 'destroy', 'chat'], not: ['start', 'extend'] }],
    [{ ...make('running', 'supported', true), expires_at: '2030-01-01T00:00:00Z' }, { has: ['extend', 'files'], not: ['start'] }],
    [make('running', 'unsupported', false), { has: ['terminal', 'stop', 'destroy'], not: ['screen', 'start'] }],
    [make('running', 'unsupported', true), { has: ['terminal', 'stop'], not: ['screen'] }],
    [make('running', 'supported', false), { has: ['terminal', 'stop'], not: ['screen'] }],
    [make('running', 'buildable', true), { has: ['screen'], not: [] }],
    [make('stopped', 'supported', true), { has: ['logs', 'start', 'destroy', 'files'], not: ['stop', 'extend', 'terminal', 'forward', 'screen'] }],
    [make('exited', 'unsupported', false), { has: ['start', 'destroy'], not: ['stop', 'screen'] }],
    [make('creating', 'unknown', false), { has: ['destroy'], not: ['stop', 'start', 'files'] }],
    [make('error', 'unsupported', false), { has: ['destroy'], not: ['stop', 'start', 'screen'] }]
  ]

  for (const [instance, expect] of cases) {
    const actions = T.instanceActions(instance)

    assert.ok(Array.isArray(actions), 'instanceActions must return an array')

    for (const action of actions) {
      assert.ok(allowed.has(action), `unknown action "${action}"`)
    }

    for (const action of expect.has) {
      assert.ok(actions.includes(action), `${instance.status}/${instance.display_state}/${instance.display}: missing "${action}" in ${actions}`)
    }

    for (const action of expect.not) {
      assert.ok(!actions.includes(action), `${instance.status}/${instance.display_state}/${instance.display}: unexpected "${action}" in ${actions}`)
    }
  }

  assert.deepEqual(new Set(T.instanceActions(DISPLAY)), new Set(['terminal', 'screen', 'forward', 'logs', 'files', 'extend', 'stop', 'destroy', 'chat']))
})

// ---- side-effecting helpers ---------------------------------------------------
await check('openTerminal: launched=true opens nothing on the clipboard', () =>
  withPlugin({}, async (ctx, fake) => {
    hostFake.hostCalls.length = 0

    const res = await T.openTerminal({ kind: 'instance', id: 'i-ab12cd34' })

    assert.equal(res?.launched, true)
    assert.ok(fake.callsTo('POST', '/instances/{id}/terminal').length === 1, 'POST /instances/i-ab12cd34/terminal expected')
    assert.equal(fake.restCalls[0].pathname, '/instances/i-ab12cd34/terminal')
    assert.deepEqual(fake.osCalls.writeClipboard, [])
    assert.deepEqual(notifyMessages(), ['Terminal opened for i-ab12cd34. It can appear behind this window.'])
  })
)

await check('openTerminal: launched=false -> clipboard + toast', () =>
  withPlugin({ 'POST /instances/{id}/terminal': { fixture: 'instance_terminal_unlaunched.json' } }, async (ctx, fake) => {
    hostFake.hostCalls.length = 0

    const expected = readFixture('instance_terminal_unlaunched.json')
    const res = await T.openTerminal({ kind: 'instance', id: 'i-ab12cd34' })

    assert.equal(res?.launched, false, 'openTerminal must return the response')
    assert.deepEqual(fake.osCalls.writeClipboard, [expected.copy_command])
    assert.ok(
      notifyMessages().includes("Couldn't open a terminal. The command is on your clipboard."),
      `host.notify messages: ${JSON.stringify(notifyMessages())}`
    )
  })
)

await check('openTerminal: host and job kinds', () =>
  withPlugin({}, async (ctx, fake) => {
    await T.openTerminal({ kind: 'host', id: 'slurm-x1' })
    await T.openTerminal({ kind: 'job', id: 'j-1a2b3c4d' })

    assert.deepEqual(
      fake.restCalls.map(c => `${c.method} ${c.pathname}`),
      ['POST /hosts/slurm-x1/terminal', 'POST /jobs/j-1a2b3c4d/terminal']
    )
  })
)

await check('openScreen: host.openWorkspace (and openExternal fallback)', () =>
  withPlugin({}, async (ctx, fake) => {
    const tunnel = readFixture('instance_display.json')

    hostFake.hostCalls.length = 0
    await T.openScreen(DISPLAY)

    const opened = callsOf('openWorkspace')

    assert.equal(opened.length, 1, 'host.openWorkspace must be called once')
    assert.equal(opened[0].args[0], 'cc-display-i-ef56aa01')
    assert.equal(typeof opened[0].args[1]?.render, 'function')
    assert.equal(typeof opened[0].args[1]?.title, 'string')
    assert.deepEqual(fake.osCalls.openExternal, [], 'no browser fallback when openWorkspace exists')

    const element = opened[0].args[1].render()

    // render() returns either the SandboxedFrame or jsx(ScreenViewer, {tunnel, instance})
    if (element?.type === sdk.SandboxedFrame) {
      assert.equal(element.props.src, tunnel.url)
      assert.ok(element.props.title)
    } else {
      assert.equal(element?.type?.displayName ?? element?.type?.name, 'ScreenViewer', 'workspace renders a SandboxedFrame or the ScreenViewer')
      assert.equal(element.props.tunnel?.url, tunnel.url)
    }
    assert.equal(fake.restCalls[0].pathname, '/instances/i-ef56aa01/display')

    removeOpenWorkspace()

    try {
      assert.equal(typeof host.openWorkspace, 'undefined')
      hostFake.hostCalls.length = 0
      await T.openScreen(DISPLAY)
      assert.deepEqual(fake.osCalls.openExternal, [tunnel.url], 'fallback must call ctx.os.openExternal(url)')
      assert.equal(callsOf('openWorkspace').length, 0)
    } finally {
      hostFake.restoreOpenWorkspace()
    }
  })
)

// ---- rendering ----------------------------------------------------------------
await check('Page renders (health configured) with tabs and Overview content', () =>
  withPlugin({}, async (ctx, fake) => {
    const tree = await mountComponent(C.Page)
    const text = allText(tree)

    for (const tab of ['Overview', 'Hosts', 'Templates', 'Jobs', 'Agents', 'Help']) {
      assert.ok(text.includes(tab), `Page is missing the "${tab}" tab`)
    }

    assert.ok(findAll(tree, n => n.name === 'Tabs').length >= 1, 'Page should use <Tabs>')
    assert.ok(fake.callsTo('GET', '/health').length >= 1, 'GET /health expected')
    assert.ok(fake.callsTo('GET', '/instances').length >= 1, 'Overview should GET /instances')
    assert.ok(fake.callsTo('GET', '/summary').length >= 1, 'Overview should GET /summary')
    assert.ok(findAll(tree, n => n.name === 'InstanceCard').length === 3, 'one InstanceCard per instance fixture')

    const queries = getQueries()

    assert.ok(queries.length >= 3 && queries.every(q => q.queryKey[0] === 'cloud-compute'), 'queries must be keyed under cloud-compute')
    assert.ok(queries.every(q => q.queryKey[1] === 'local' && q.queryKey[2] === 'default'), 'query keys carry [connectionId, profile]')
  })
)

await check('Page: every tab renders, switching tabs triggers only fixture routes', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.Page)

    for (const tab of ['hosts', 'templates', 'jobs', 'agents', 'help', 'overview']) {
      tree = await selectTab(tree, tab)
      assert.ok(textOf(tree).length > 0)
    }

    assert.ok(fake.callsTo('GET', '/hosts').length >= 1)
    assert.ok(fake.callsTo('GET', '/templates').length >= 1)
    assert.ok(fake.callsTo('GET', '/jobs').length >= 1)
    assert.ok(fake.callsTo('GET', '/host-types').length >= 1)
    assert.ok(fake.callsTo('GET', '/agents').length >= 1, 'the Agents tab must GET /agents')
  })
)

await check('missing backend (/health 404 and 403) -> enable hint', async () => {
  for (const status of [404, 403]) {
    await withPlugin({ 'GET /health': { status } }, async () => {
      const tree = await mountComponent(C.Page)
      const text = allText(tree)

      assert.ok(text.includes('hermes plugins enable cloud-compute'), `HTTP ${status}: text was "${text.slice(0, 200)}"`)
      assert.ok(text.includes('restart Desktop'), 'hint should mention restarting Desktop')
    })
  }
})

await check('other /health error -> ErrorState with Retry', () =>
  withPlugin({ 'GET /health': { status: 500, body: { error: { code: 'internal', message: 'boom' } } } }, async () => {
    const tree = await mountComponent(C.Page)

    assert.ok(findAll(tree, n => n.name === 'ErrorState').length === 1, 'ErrorState expected')
    assert.ok(findButtons(tree).byLabel('Retry'), 'Retry button expected')
    assert.ok(!allText(tree).includes('hermes plugins enable cloud-compute'))
  })
)

await check('configured:false -> first-run EmptyState and wizard', () =>
  withPlugin({ 'GET /health': { fixture: 'health_unconfigured.json' } }, async (ctx, fake) => {
    const tree = await mountComponent(C.Page)
    const text = allText(tree)

    assert.equal(findAll(tree, n => n.name === 'EmptyState').length, 1, 'first-run EmptyState expected')
    assert.ok(findAll(tree, n => n.name === 'AddHostWizard').length >= 1, 'first-run wizard expected')
    assert.ok(text.includes('SSH host') && text.includes('Slurm login node'), 'wizard step 1 shows the host type cards')
    // Help stays reachable before the first host exists (before the first host exists).
    assert.deepEqual(findAll(tree, n => n.name === 'TabsTrigger').map(n => textOf(n)), ['Get started', 'Help'])
  })
)

await check('OverviewTab: top bar, instance cards, job rows', () =>
  withPlugin({}, async (ctx, fake) => {
    const tree = await mountComponent(C.OverviewTab)
    const labels = labelsOf(tree)

    for (const label of ['Stop all', 'Refresh']) {
      assert.ok(labels.includes(label), `missing top-bar button "${label}" (have ${labels})`)
    }

    const cards = findAll(tree, n => n.name === 'InstanceCard')

    assert.equal(cards.length, 3)

    for (const card of cards) {
      const instance = card.props.instance ?? card.props.item
      const buttons = findButtons(card).labels()
      const actions = T.instanceActions(instance)

      assert.ok(instance?.id, 'InstanceCard needs an `instance` (or `item`) prop')

      for (const action of actions) {
        assert.ok(buttons.includes(BUTTON_LABELS[action]), `${instance.id}: missing "${BUTTON_LABELS[action]}" (have ${buttons})`)
      }

      for (const [action, label] of Object.entries(BUTTON_LABELS)) {
        if (!actions.includes(action)) {
          assert.ok(!buttons.includes(label), `${instance.id}: unexpected "${label}" button`)
        }
      }
    }

    // explicit spec cases
    const screenOf = id => findButtons(cards.find(c => (c.props.instance ?? c.props.item).id === id)).labels().includes('Screen')

    assert.equal(DISPLAY.display_state, 'supported')
    assert.equal(screenOf('i-ef56aa01'), true, 'supported + display:true instance shows Screen')
    assert.equal(PLAIN.display_state, 'unsupported')
    assert.equal(screenOf('i-ab12cd34'), false, 'unsupported instance has no Screen')
    assert.equal(screenOf('i-0c9d8e7f'), false, 'stopped unsupported instance has no Screen')
    assert.ok(allText(tree).includes('train-small'), 'active job rows come from GET /jobs?active=1')
    assert.ok(fake.restCalls.some(c => c.pathname === '/jobs' && c.query.active === '1'), 'Overview should request /jobs?active=1')
  })
)

await check('InstanceCard standalone: supported vs unsupported', () =>
  withPlugin({}, async () => {
    const supported = await mountComponent(C.InstanceCard, { instance: DISPLAY })
    const unsupported = await mountComponent(C.InstanceCard, { instance: PLAIN })

    assert.ok(findButtons(supported).labels().includes('Screen'), 'supported + display:true -> Screen')
    assert.ok(!findButtons(unsupported).labels().includes('Screen'), 'unsupported -> no Screen')
    assert.ok(findButtons(unsupported).labels().includes('Stop'), 'running -> Stop')
    assert.ok(findButtons(await mountComponent(C.InstanceCard, { instance: STOPPED })).labels().includes('Start'), 'stopped -> Start')
  })
)

await check('InstanceCard actions call the right routes', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.InstanceCard, { instance: DISPLAY })

    tree = await click(findButtons(tree).byLabel('Terminal'))
    tree = await click(findButtons(tree).byLabel('Screen'))
    tree = await click(findButtons(tree).byLabel('Stop'))

    const calls = fake.restCalls.map(c => `${c.method} ${c.pathname}`)

    assert.ok(calls.includes('POST /instances/i-ef56aa01/terminal'), calls.join(', '))
    assert.ok(calls.includes('POST /instances/i-ef56aa01/display'), calls.join(', '))
    assert.ok(calls.includes('POST /instances/i-ef56aa01/stop'), calls.join(', '))
    assert.equal(callsOf('openWorkspace').length, 1)
  })
)

await check('InstanceCard: Delete asks first and calls the API only after the dialog is confirmed', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.InstanceCard, { instance: PLAIN })

    tree = await click(findButtons(tree).byLabel('Delete'))
    assert.ok(!fake.restCalls.some(c => c.pathname.endsWith('/destroy')), 'delete must wait for the dialog')
    assert.ok(findAll(tree, n => n.name === 'DeleteDialog').length === 1, 'a DeleteDialog should be shown')
  })
)

await check('HostsTab: cards, statuses and per-host buttons', () =>
  withPlugin({}, async (ctx, fake) => {
    const tree = await mountComponent(C.HostsTab)
    const text = allText(tree)
    const hosts = readFixture('hosts.json')

    assert.ok(findButtons(tree).labels().includes('+ Add host'), 'missing "+ Add host"')

    for (const { spec } of hosts) {
      assert.ok(text.includes(spec.name), `host "${spec.name}" is not shown`)
    }

    for (const word of ['Online', 'Degraded', 'Offline', 'Needs login']) {
      assert.ok(text.includes(word), `status light label "${word}" not shown`)
    }

    const cards = findAll(tree, n => n.name === 'HostCard')

    assert.equal(cards.length, hosts.length)

    for (const card of cards) {
      const item = card.props.item ?? card.props.entry ?? card.props.hostItem
      const labels = findButtons(card).labels()
      const spec = item.spec ?? item
      const caps = item.state?.capabilities

      for (const label of ['Refresh status', 'Test connection', 'Edit', 'Remove']) {
        assert.ok(labels.includes(label), `${spec.id}: missing "${label}" (have ${labels})`)
      }

      if (spec.kind !== 'slurm') {
        assert.ok(!labels.includes('Open shell'), `${spec.id}: "Open shell" is for Slurm login nodes only`)
      }

      assert.equal(labels.includes('Build display image'), caps?.display === 'buildable' && item.state?.status === 'online', `${spec.id}: Build display image only when buildable`)
    }

    const slurmOnline = cards.find(c => (c.props.item ?? c.props.entry ?? c.props.hostItem).spec.id === 'slurm-x1')

    assert.ok(findButtons(slurmOnline).labels().includes('Open shell'), 'online Slurm host shows "Open shell"')

    // Refresh status -> POST /hosts/{id}/probe
    const x1 = cards.find(c => (c.props.item ?? c.props.entry ?? c.props.hostItem).spec.id === 'x1carbon')

    await click(findButtons(x1).byLabel('Refresh status'))
    assert.ok(fake.restCalls.some(c => c.method === 'POST' && c.pathname === '/hosts/x1carbon/probe'), 'Refresh status -> POST /hosts/x1carbon/probe')
  })
)

await check('HostsTab: + Add host opens the wizard with the host type cards', () =>
  withPlugin({}, async () => {
    let tree = await mountComponent(C.HostsTab)

    tree = await click(findButtons(tree).byLabel('+ Add host'))

    assert.ok(findAll(tree, n => n.name === 'AddHostWizard').length >= 1, 'AddHostWizard should be open')

    const text = allText(tree)

    for (const title of ['SSH host', 'Slurm login node', 'Local Docker/Podman']) {
      assert.ok(text.includes(title), `wizard step 1 should show "${title}"`)
    }
  })
)

await check('capability chips follow the host-type catalog order and labels', () =>
  withPlugin({}, async () => {
    const tree = await mountComponent(C.AddHostWizard, { onClose() {}, onFinished() {} })
    const text = allText(tree)
    const order = ['Containers', 'Terminal', 'Screen', 'Port forward', 'GPU', 'Cloud agents']
    let at = -1

    for (const label of order) {
      const next = text.indexOf(label, at + 1)

      assert.ok(next > at, 'chip ' + label + ' expected after the previous chip in: ' + text.slice(0, 400))
      at = next
    }

    assert.ok(text.includes('Costs money'), 'EC2 card shows Costs money')
    assert.ok(!text.includes('(partial)'), 'no chip says (partial)')
    assert.ok(!/partial/i.test(text), 'the word partial is not shown to the user')
  })
)

await check('AddHostWizard renders standalone', () =>
  withPlugin({}, async () => {
    const tree = await mountComponent(C.AddHostWizard, { onClose() {}, onFinished() {} })

    assert.ok(allText(tree).includes('SSH host'))
  })
)

await check('HostCard renders standalone', () =>
  withPlugin({}, async () => {
    const [first] = readFixture('hosts.json')
    const tree = await mountComponent(C.HostCard, { item: first })

    assert.ok(allText(tree).includes(first.spec.name))
  })
)

await check('TemplatesTab: template cards and Spawn on…', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.TemplatesTab)
    const text = allText(tree)

    for (const { name } of readFixture('templates.json')) {
      assert.ok(text.includes(name), `template "${name}" not shown`)
    }

    const spawn = findButtons(tree).allByLabel('Spawn on…')

    assert.ok(spawn.length >= 1, 'missing "Spawn on…" buttons')

    tree = await click(spawn[0])
    assert.ok(textOf(tree).length > text.length - 1, 'Spawn on… should open a host picker')
    assert.ok(fake.callsTo('GET', '/hosts').length >= 1, 'host picker needs GET /hosts')
  })
)

await check('JobsTab renders', () =>
  withPlugin({}, async () => {
    const tree = await mountComponent(C.JobsTab)

    assert.ok(textOf(tree).length > 0)
  })
)

await check('SettingsPage renders', () =>
  withPlugin({}, async () => {
    const tree = await mountComponent(C.SettingsPage)

    assert.ok(textOf(tree).length > 0)
  })
)

await check('StatusChip renders chip text and navigates on click', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.StatusChip)
    const summary = readFixture('summary.json')
    const model = T.chipModel(summary)

    assert.equal(allText(tree), model.text)
    assert.ok(findAll(tree, n => n.name === 'StatusDot').length === 1, 'chip shows a StatusDot')
    assert.ok(findAll(tree, n => n.name === 'StatusDot')[0].props.tone, 'StatusDot needs a tone')

    const [button] = findAll(tree, n => n.type === 'button' || n.name === 'Button')

    assert.ok(button?.props.onClick, 'chip must be clickable')
    hostFake.hostCalls.length = 0
    await click(button)
    assert.deepEqual(callsOf('navigate').map(c => c.args[0]), ['/cloud-compute'])
  })
)

await check('StatusChip with no hosts / missing backend', async () => {
  await withPlugin({ 'GET /summary': { body: { hosts: { total: 0, online: 0, degraded: 0, auth_required: 0, offline: 0 }, instances: { running: 0, total: 0 }, jobs: { active: 0, finished_24h: 0 }, attention: [], generated_at: '2026-11-08T14:30:00Z' } } }, async () => {
    assert.equal(allText(await mountComponent(C.StatusChip)), 'No hosts')
  })
  await withPlugin({ 'GET /summary': { status: 404 } }, async () => {
    await mountComponent(C.StatusChip)
  })
})

const FOLLOWUP_LINE = 'Already enabled? Restart the Hermes gateway'

await check('followup: SettingsPage with /health 404 shows the friendly message, not Not Found', () =>
  withPlugin({ 'GET /health': { status: 404 } }, async () => {
    const text = allText(await mountComponent(C.SettingsPage))

    assert.ok(text.includes('hermes plugins enable cloud-compute'), `text was "${text.slice(0, 200)}"`)
    assert.ok(text.includes(FOLLOWUP_LINE), 'follow-up line expected')
    assert.ok(!text.includes('Not Found'), 'raw error must not show')
  })
)

await check('followup: SettingsPage with a healthy backend still lists hosts', () =>
  withPlugin({}, async (ctx, fake) => {
    const text = allText(await mountComponent(C.SettingsPage))

    assert.ok(!text.includes('hermes plugins enable cloud-compute'))
    assert.ok(fake.callsTo('GET', '/health').length >= 1)
    assert.ok(/\d+ hosts?/.test(text), `hosts expected, text was "${text.slice(0, 300)}"`)
  })
)

await check('followup: Page missing backend shows the follow-up line', () =>
  withPlugin({ 'GET /health': { status: 404 } }, async () => {
    const text = allText(await mountComponent(C.Page))

    assert.ok(text.includes('restart Desktop') && text.includes(FOLLOWUP_LINE))
  })
)

await check('followup: StatusChip renders nothing on 404 and a neutral label on 500', async () => {
  await withPlugin({ 'GET /summary': { status: 404 } }, async () => {
    assert.equal(allText(await mountComponent(C.StatusChip)), '')
  })
  await withPlugin({ 'GET /summary': { status: 500, body: { error: { code: 'internal', message: 'boom' } } } }, async () => {
    assert.equal(allText(await mountComponent(C.StatusChip)), 'Compute')
  })
})

await check('HelpTab: type cards, one-liners and Learn more', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.HelpTab)
    const text = allText(tree)
    const cards = readFixture('host_types.json')

    for (const card of cards) {
      assert.ok(text.includes(card.title), `type card "${card.title}" missing`)
      assert.ok(text.includes(card.one_liner), `one-liner of "${card.title}" missing`)
    }

    const more = findButtons(tree).allByLabel('Learn more')

    assert.ok(more.length >= 4, `expected a "Learn more" button per type card, got ${more.length}`)
    assert.ok(fake.callsTo('GET', '/host-types').length >= 1)

    tree = await click(more[0])
    assert.ok(fake.restCalls.some(c => c.method === 'GET' && c.pathname.startsWith('/docs/')), 'Learn more should GET /docs/{topic}')
    assert.ok(allText(tree).includes('SSH hosts') || allText(tree).includes('Requirements'), 'docs dialog shows the topic')
  })
)

await check('Help falls back to embedded copy when /host-types is unavailable', () =>
  withPlugin({ 'GET /host-types': { status: 404 } }, async () => {
    const tree = await mountComponent(C.HelpTab)
    const text = allText(tree)

    for (const card of Object.values(T.COPY.type_cards ?? {})) {
      assert.ok(text.includes(card.one_liner), `embedded one-liner of "${card.title}" missing`)
    }
  })
)

// =============================================================================
// Contract checks: jobs, screen viewer, settings, notifications, palette, chat card, tunnels.
// =============================================================================
const JOB_REQUEST_KEYS = readFixture('job_request_keys.json')
const jobsFixture = readFixture('jobs.json')
const jobByName = Object.fromEntries(jobsFixture.map(j => [j.name, j]))
const RUNNING_JOB = jobByName['train-small']
const PENDING_JOB = jobByName['sweep-array']
const DONE_JOB = jobByName['preprocess']
const FAILED_JOB = jobByName['eval-broken']
const norm = value => String(value ?? '').toLowerCase().replace(/[^a-z0-9]/g, '')
const restLines = fake => fake.restCalls.map(c => `${c.method} ${c.pathname}${Object.keys(c.query).length ? `?${new URLSearchParams(c.query)}` : ''}`)
const bodyOf = call => (typeof call?.body === 'string' ? JSON.parse(call.body) : call?.body)
const needFn = (obj, name, where) => assert.equal(typeof obj?.[name], 'function', `${where}.${name} is missing`)
const BUTTON_FOR_JOB_ACTION = { output: 'Output', cancel: 'Cancel', terminal: 'Terminal', delete: 'Remove from list' }

/** Smallest subtree that mentions `text` (and no name from `others`) and holds at least one button. */
function rowOf(tree, text, others) {
  const found = findAll(tree, n => {
    if (n === null || typeof n !== 'object' || n.type === undefined) {
      return false
    }

    const t = textOf(n)

    return t.includes(text) && others.every(o => !t.includes(o)) && findButtons(n).length > 0
  })

  found.sort((a, b) => textOf(a).length - textOf(b).length)

  return found[0]
}

/** Input-like nodes in document order with the label text that precedes them. */
function fieldNodes(tree) {
  const out = []
  let last = ''

  const visit = node => {
    if (node === null || node === undefined) {
      return
    }

    if (typeof node === 'string') {
      if (node.trim()) {
        last = node
      }

      return
    }

    const p = node.props ?? {}
    const handler = ['onChange', 'onValueChange', 'onCheckedChange'].some(k => typeof p[k] === 'function')

    if (handler) {
      out.push({ node, label: norm(last), attrs: [p.name, p.id, p['aria-label'], p.placeholder, p['data-field'], p.label].map(norm).filter(Boolean) })
    }

    const saved = last

    for (const child of node.children ?? []) {
      visit(child)
    }

    if (handler) {
      last = saved
    }
  }

  visit(tree)

  return out
}

function findField(tree, keys, where) {
  const wanted = new Set(keys.map(norm))
  const hit = fieldNodes(tree).find(f => f.attrs.some(a => wanted.has(a)) || [...wanted].some(k => f.label.endsWith(k)))

  assert.ok(hit, `${where}: no input for ${keys[0]} (accepted name/id/aria-label/placeholder/label: ${[...wanted]}); fields seen: ${fieldNodes(tree).map(f => f.attrs[0] ?? f.label).join(', ')}`)

  return hit.node
}

const openDialog = (tree, name) => findAll(tree, n => n.name === name && n.props?.open !== false)[0]
const openConfirm = tree => findAll(tree, n => n.name === 'ConfirmDialog' && n.props?.open !== false)[0]

// ---- 1 + 2: API surface ------------------------------------------------------
await check('API_CALLS has 58 entries incl. GET /instances/{id} and the four /agents calls', () => {
  const calls = T.API_CALLS.map(c => `${c.method} ${c.path}`)

  assert.ok(calls.includes('GET /instances/{id}'), 'GET /instances/{id} missing from __test__.API_CALLS')
  assert.equal(calls.length, 58, `API_CALLS has ${calls.length} entries, expected 58`)

  for (const call of ['GET /agents', 'POST /agents', 'POST /agents/{id}/collect', 'POST /agents/{id}/stop']) {
    assert.ok(calls.includes(call), `${call} missing from __test__.API_CALLS`)
  }
})

await check('__test__.components has SubmitJobDialog, ScreenViewer, ChatCard', () => {
  for (const name of ['SubmitJobDialog', 'ScreenViewer', 'ChatCard']) {
    assert.equal(typeof C[name], 'function', `__test__.components.${name} must be a function`)
  }
})

await check('jobActions(job) rules', () => {
  needFn(T, 'jobActions', '__test__')

  const expected = {
    PENDING: ['output', 'cancel'],
    RUNNING: ['output', 'cancel', 'terminal'],
    COMPLETING: ['output', 'cancel'],
    COMPLETED: ['output', 'delete'],
    FAILED: ['output', 'delete'],
    CANCELLED: ['output', 'delete'],
    TIMEOUT: ['output', 'delete'],
    OUT_OF_MEMORY: ['output', 'delete'],
    NODE_FAIL: ['output', 'delete'],
    UNKNOWN: ['output']
  }

  for (const [state, want] of Object.entries(expected)) {
    const got = T.jobActions({ id: 'j-00000000', state })

    assert.ok(Array.isArray(got), `jobActions(${state}) must return an array`)
    assert.deepEqual([...got].sort(), [...want].sort(), `jobActions(${state}) -> ${JSON.stringify(got)}, expected ${JSON.stringify(want)}`)
  }

  assert.deepEqual([...T.jobActions(RUNNING_JOB)].sort(), ['cancel', 'output', 'terminal'])
})

// ---- 3: Jobs tab ---------------------------------------------------------------
await check('JobsTab lists jobs (GET /jobs, GET /hosts?kind=slurm) with per-row buttons from jobActions', () =>
  withPlugin({}, async (ctx, fake) => {
    needFn(T, 'jobActions', '__test__')

    const tree = await mountComponent(C.JobsTab)
    const lines = restLines(fake)

    assert.ok(lines.includes('GET /jobs'), `JobsTab must GET /jobs (calls: ${lines})`)
    assert.ok(lines.includes('GET /hosts?kind=slurm'), `JobsTab must GET /hosts?kind=slurm (calls: ${lines})`)
    assert.ok(findButtons(tree).labels().includes('Submit job…'), `JobsTab needs a "Submit job…" button (have ${findButtons(tree).labels()})`)

    const names = jobsFixture.map(j => j.name)

    for (const job of jobsFixture) {
      const row = rowOf(tree, job.name, names.filter(n => n !== job.name))

      assert.ok(row, `no row with buttons found for job "${job.name}" (page text: ${textOf(tree).slice(0, 200)})`)

      const labels = findButtons(row).labels()
      const actions = T.jobActions(job)

      for (const [action, label] of Object.entries(BUTTON_FOR_JOB_ACTION)) {
        assert.equal(labels.includes(label), actions.includes(action), `${job.name} (${job.state}): "${label}" button should ${actions.includes(action) ? '' : 'not '}be present (row buttons: ${labels})`)
      }
    }

    assert.ok(PENDING_JOB && RUNNING_JOB && DONE_JOB && FAILED_JOB, 'fixtures must hold pending, running, completed and failed jobs')
  })
)

await check('JobsTab Output button -> GET /jobs/{id}/output?tail=200&stream=out, out/err toggle', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.JobsTab)
    const row = () => rowOf(tree, RUNNING_JOB.name, jobsFixture.map(j => j.name).filter(n => n !== RUNNING_JOB.name))
    const outputCalls = stream =>
      fake.restCalls.filter(c => c.method === 'GET' && c.pathname === `/jobs/${RUNNING_JOB.id}/output` && c.query.tail === '200' && c.query.stream === stream)

    assert.ok(row(), 'no row for the running job')
    tree = await click(findButtons(row()).byLabel('Output'))
    assert.equal(outputCalls('out').length >= 1, true, `Output must GET /jobs/${RUNNING_JOB.id}/output?tail=200&stream=out (calls: ${restLines(fake)})`)
    assert.ok(allText(tree).includes('epoch 2/3 loss=0.64'), 'job output text is not displayed')

    const toggle = findButtons(tree).find(b => /^(err|stderr)$/i.test(b.label))

    assert.ok(toggle, `an "err"/"stderr" toggle is expected (buttons: ${findButtons(tree).labels()})`)
    tree = await click(toggle)
    assert.ok(outputCalls('err').length >= 1, `err toggle must GET ...output?tail=200&stream=err (calls: ${restLines(fake)})`)
    assert.ok(findButtons(tree).some(b => /^(out|stdout)$/i.test(b.label)), 'an "out"/"stdout" toggle is expected')
  })
)

await check('JobsTab Cancel goes through a ConfirmDialog before POST /jobs/{id}/cancel', () =>
  withPlugin({}, async (ctx, fake) => {
    const others = jobsFixture.map(j => j.name).filter(n => n !== RUNNING_JOB.name)
    let tree = await mountComponent(C.JobsTab)

    tree = await click(findButtons(rowOf(tree, RUNNING_JOB.name, others)).byLabel('Cancel'))
    assert.ok(!fake.restCalls.some(c => c.pathname.endsWith('/cancel')), 'POST .../cancel was sent before the confirmation')

    const dialog = openConfirm(tree)

    assert.ok(dialog, 'Cancel must open a ConfirmDialog')
    await dialog.props.onConfirm()
    await dialog.session.settle()

    const posts = fake.restCalls.filter(c => c.method === 'POST' && c.pathname === `/jobs/${RUNNING_JOB.id}/cancel`)

    assert.equal(posts.length, 1, `confirming must POST /jobs/${RUNNING_JOB.id}/cancel once (calls: ${restLines(fake)})`)
  })
)

await check('JobsTab Terminal button -> openTerminal({kind:"job"}) -> POST /jobs/{id}/terminal', () =>
  withPlugin({}, async (ctx, fake) => {
    const others = jobsFixture.map(j => j.name).filter(n => n !== RUNNING_JOB.name)
    const tree = await mountComponent(C.JobsTab)

    await click(findButtons(rowOf(tree, RUNNING_JOB.name, others)).byLabel('Terminal'))
    assert.ok(restLines(fake).includes(`POST /jobs/${RUNNING_JOB.id}/terminal`), `calls: ${restLines(fake)}`)
  })
)

await check('SubmitJobDialog posts a JobRequest with exactly the JobRequest keys and the documented defaults', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.JobsTab)

    tree = await click(findButtons(tree).byLabel('Submit job…'))

    const dialog = () => openDialog(tree, 'SubmitJobDialog')

    assert.ok(dialog(), 'clicking "Submit job…" must open SubmitJobDialog')

    tree = await setValue(findField(dialog(), ['host', 'host_id', 'slurm host'], 'SubmitJobDialog'), 'slurm-x1')
    tree = await setValue(findField(dialog(), ['name', 'job name', 'job_name'], 'SubmitJobDialog'), 'smoke-job')
    tree = await setValue(findField(dialog(), ['command'], 'SubmitJobDialog'), 'echo hello')

    assert.ok(restLines(fake).includes('GET /hosts/slurm-x1/partitions'), `choosing a host must GET /hosts/{id}/partitions (calls: ${restLines(fake)})`)

    const labels = findButtons(dialog()).labels()
    const submit = findButtons(dialog()).find(b => /^(submit|submit job|run|launch)$/i.test(b.label))

    assert.ok(submit, `SubmitJobDialog needs a Submit button (buttons: ${labels})`)
    await click(submit)

    const posts = fake.restCalls.filter(c => c.method === 'POST' && c.pathname === '/jobs')

    assert.equal(posts.length, 1, `exactly one POST /jobs expected (calls: ${restLines(fake)})`)

    const body = bodyOf(posts[0])

    assert.deepEqual(Object.keys(body).sort(), [...JOB_REQUEST_KEYS].sort(), 'POST /jobs body keys must equal the JobRequest fields')
    assert.equal(body.host_id, 'slurm-x1')
    assert.equal(body.name, 'smoke-job')
    assert.equal(body.command, 'echo hello')
    assert.ok(body.script === null || body.script === undefined || body.script === '', `script must be empty when a command is given (got ${JSON.stringify(body.script)})`)
    assert.equal(body.cpus, 1, 'default cpus is the number 1')
    assert.equal(body.mem_gb, 1, 'default mem_gb is the number 1')
    assert.equal(body.gpus, 0, 'default gpus is the number 0')
    assert.equal(body.time_limit, '00:30:00')
  })
)

await check('JobsTab with no Slurm hosts shows an empty state with "+ Add host"', () =>
  withPlugin({ 'GET /hosts': call => (call.query.kind === 'slurm' ? [] : readFixture('hosts.json')) }, async () => {
    let tree = await mountComponent(C.JobsTab)

    assert.ok(findAll(tree, n => n.name === 'EmptyState').length >= 1, 'an EmptyState is expected when there are no Slurm hosts')
    assert.ok(findButtons(tree).labels().includes('+ Add host'), `"+ Add host" expected (buttons: ${findButtons(tree).labels()})`)
    tree = await click(findButtons(tree).byLabel('+ Add host'))
    assert.ok(findAll(tree, n => n.name === 'AddHostWizard').length >= 1, '"+ Add host" must open the wizard')
  })
)

// ---- 4: screen viewer ----------------------------------------------------------
await check('openScreen renders ScreenViewer {tunnel, instance} with Open in browser / Stop screen / SandboxedFrame', () =>
  withPlugin({}, async (ctx, fake) => {
    const tunnel = readFixture('instance_display.json')

    hostFake.hostCalls.length = 0
    await T.openScreen(DISPLAY)

    const [opened] = callsOf('openWorkspace')

    assert.ok(opened, 'host.openWorkspace was not called')

    const element = opened.args[1].render()

    assert.ok(C.ScreenViewer, '__test__.components.ScreenViewer is missing')
    assert.equal(element?.type, C.ScreenViewer, 'render() must return jsx(ScreenViewer, {tunnel, instance})')
    assert.equal(element.props.tunnel?.url, tunnel.url)
    assert.equal(element.props.instance?.id, DISPLAY.id)

    let tree = await renderUntilStable(element)
    const frames = findAll(tree, n => n.name === 'SandboxedFrame')

    assert.equal(frames.length, 1, 'ScreenViewer must render one SandboxedFrame')
    assert.equal(frames[0].props.src, tunnel.url)
    assert.ok(!String(frames[0].props.sandbox ?? '').includes('allow-same-origin'), 'sandbox must never include allow-same-origin')

    const labels = findButtons(tree).labels()

    for (const label of ['Open in browser', 'Stop screen']) {
      assert.ok(labels.includes(label), `ScreenViewer is missing "${label}" (have ${labels})`)
    }

    await click(findButtons(tree).byLabel('Open in browser'))
    assert.deepEqual(fake.osCalls.openExternal, [tunnel.url], 'Open in browser -> ctx.os.openExternal(tunnel.url)')

    const before = fake.restCalls.length

    tree = await click(findButtons(tree).byLabel('Stop screen'))

    const after = restLines({ restCalls: fake.restCalls.slice(before) })

    assert.deepEqual(after.slice(0, 1), [`DELETE /tunnels/${tunnel.id}`], `Stop screen must DELETE /tunnels/${tunnel.id} (calls: ${after})`)
  })
)

await check('ScreenViewer shows the blank-screen hint and the Open in browser button', () =>
  withPlugin({}, async (ctx, fake) => {
    const tunnel = readFixture('instance_display.json')

    const tree = await renderUntilStable(jsx(C.ScreenViewer, { tunnel, instance: DISPLAY }))

    assert.ok(allText(tree).includes('If the screen stays blank, use Open in browser.'), 'ScreenViewer must show the blank-screen hint')
    assert.ok(findButtons(tree).byLabel('Open in browser'), `ScreenViewer needs an "Open in browser" button (have ${findButtons(tree).labels()})`)
  })
)

await check('openScreen without host.openWorkspace still falls back to ctx.os.openExternal(url)', () =>
  withPlugin({}, async (ctx, fake) => {
    removeOpenWorkspace()

    try {
      await T.openScreen(DISPLAY)
      assert.deepEqual(fake.osCalls.openExternal, [readFixture('instance_display.json').url])
    } finally {
      hostFake.restoreOpenWorkspace()
    }
  })
)

// ---- 5: settings page ------------------------------------------------------------
await check('settings page id/title, GET /hosts and "Open Compute" -> host.navigate("/cloud-compute")', () =>
  withPlugin({}, async (ctx, fake) => {
    const page = fake.byArea('settings.plugins')[0]

    assert.ok(page, 'ctx.registerSettingsPage was not called')
    assert.equal(page.id, 'cloud-compute:cloud-compute')
    assert.equal(page.title, 'Own Compute')

    let tree = await renderUntilStable(page.render())

    assert.ok(fake.callsTo('GET', '/hosts').length >= 1, 'the settings page must GET /hosts')

    const open = findButtons(tree).byLabel('Open Compute')

    assert.ok(open, `settings page needs an "Open Compute" button (have ${findButtons(tree).labels()})`)
    hostFake.hostCalls.length = 0
    await click(open)
    assert.deepEqual(callsOf('navigate').map(c => c.args[0]), ['/cloud-compute'])
    assert.ok(allText(tree).includes(readFixture('hosts.json')[0].spec.name), 'settings page should list the configured hosts')
  })
)

// ---- 6: notifications -------------------------------------------------------------
const invalidatedOurs = () => queryClient.calls.some(c => c.method === 'invalidateQueries' && c.filters?.queryKey?.[0] === 'cloud-compute')

await check('job.finished -> host.notify toast naming job and state AND ctx.os.notify', () =>
  withPlugin({}, async (ctx, fake) => {
    queryClient.reset()
    hostFake.hostCalls.length = 0
    fake.emit('plugin.cloud-compute.job.finished', { id: 'j-99aa11bb', host_id: 'slurm-x1', slurm_job_id: '41', name: 'preprocess', state: 'COMPLETED', exit_code: '0:0' })

    const toasts = toastTexts()

    assert.equal(toasts.length >= 1, true, 'no host.notify toast')
    assert.ok(toasts.some(t => t.includes('preprocess') && t.toLowerCase().includes('completed')), `toast must mention name and state (case-insensitive): ${JSON.stringify(toasts)}`)
    assert.ok(fake.osCalls.notify.length >= 1, 'ctx.os.notify was not called')
    assert.ok(invalidatedOurs(), 'job.finished must still invalidate ["cloud-compute"]')
  })
)

await check('host.status to offline (prev not offline) -> toast + ctx.os.notify', () =>
  withPlugin({}, async (ctx, fake) => {
    for (const prev of ['online', 'degraded']) {
      queryClient.reset()
      hostFake.hostCalls.length = 0
      fake.osCalls.notify.length = 0
      fake.emit('plugin.cloud-compute.host.status', { id: 'lab-gpu', status: 'offline', prev, last_error: 'timed out' })
      assert.ok(toastTexts().length >= 1, `${prev} -> offline: no toast`)
      assert.ok(toastTexts().some(t => t.includes('lab-gpu')), `toast should name the host: ${JSON.stringify(toastTexts())}`)
      assert.ok(fake.osCalls.notify.length >= 1, `${prev} -> offline: ctx.os.notify was not called`)
      assert.ok(invalidatedOurs(), 'host.status must still invalidate')
    }
  })
)

await check('host.status offline that is expected (an EC2 machine that is stopped or absent) does NOT toast or os.notify', () =>
  withPlugin({}, async (ctx, fake) => {
    queryClient.reset()
    hostFake.hostCalls.length = 0
    fake.osCalls.notify.length = 0
    fake.emit('plugin.cloud-compute.host.status', { id: 'aws-b', status: 'offline', prev: 'unknown', last_error: 'The machine is stopped.', expected: true })
    assert.deepEqual(toastTexts(), [])
    assert.deepEqual(fake.osCalls.notify, [])
    assert.ok(invalidatedOurs(), 'the card still refreshes')
  })
)

await check('host.status online->online, offline->offline, ->online do NOT toast or os.notify', () =>
  withPlugin({}, async (ctx, fake) => {
    for (const [status, prev] of [['online', 'online'], ['offline', 'offline'], ['online', 'offline'], ['degraded', 'online']]) {
      queryClient.reset()
      hostFake.hostCalls.length = 0
      fake.osCalls.notify.length = 0
      fake.emit('plugin.cloud-compute.host.status', { id: 'lab-gpu', status, prev, last_error: null })
      assert.deepEqual(toastTexts(), [], `${prev} -> ${status} must not toast`)
      assert.deepEqual(fake.osCalls.notify, [], `${prev} -> ${status} must not call ctx.os.notify`)
      assert.ok(invalidatedOurs(), `${prev} -> ${status} must still invalidate`)
    }
  })
)

await check('instances.changed reason "error" -> toast', () =>
  withPlugin({}, async (ctx, fake) => {
    queryClient.reset()
    hostFake.hostCalls.length = 0
    fake.emit('plugin.cloud-compute.instances.changed', { ids: ['i-ab12cd34'], host_id: 'x1carbon', reason: 'error' })
    assert.ok(toastTexts().length >= 1, 'no toast for reason "error"')
    assert.ok(invalidatedOurs())

    hostFake.hostCalls.length = 0
    fake.emit('plugin.cloud-compute.instances.changed', { ids: ['i-ab12cd34'], host_id: 'x1carbon', reason: 'stats' })
    assert.deepEqual(toastTexts(), [], 'reason "stats" must not toast')
  })
)

await check('plugin.cloud-compute.notify -> toast', () =>
  withPlugin({}, async (ctx, fake) => {
    queryClient.reset()
    hostFake.hostCalls.length = 0
    fake.emit('plugin.cloud-compute.notify', { level: 'warn', title: 'Disk almost full', body: 'x1carbon has 2 GB left', ref: { kind: 'host', id: 'x1carbon' } })

    const toasts = toastTexts()

    assert.equal(toasts.length >= 1, true, 'no host.notify toast for plugin.cloud-compute.notify')
    assert.ok(toasts.some(t => t.includes('Disk almost full') || t.includes('x1carbon has 2 GB left')), `toast should carry the title or body: ${JSON.stringify(toasts)}`)
    assert.ok(invalidatedOurs())
  })
)

// ---- 7: palette ------------------------------------------------------------------
await check('every palette contribution has run() that navigates to /cloud-compute', () =>
  withPlugin({}, async (ctx, fake) => {
    const palette = fake.byArea(sdk.PALETTE_AREA)

    assert.equal(palette.length, 5)

    for (const entry of palette) {
      assert.equal(typeof entry.data?.run, 'function', `${entry.id}: data.run must be a function`)
      hostFake.hostCalls.length = 0
      await entry.data.run()
      assert.equal(callsOf('navigate').at(-1)?.args[0], '/cloud-compute', `${entry.id}: run() must host.navigate('/cloud-compute') (navigate calls: ${JSON.stringify(callsOf('navigate').map(c => c.args))})`)
    }
  })
)

// ---- 8: chat card directive -------------------------------------------------------
const directive = fake => fake.find(sdk.TRANSCRIPT_DIRECTIVE_AREA, 'cc-env-card')

await check('directive registered as cloud-compute:cc-env-card, name "cc-env", data.render', () =>
  withPlugin({}, async (ctx, fake) => {
    const entry = directive(fake)

    assert.ok(entry, `TRANSCRIPT_DIRECTIVE_AREA (${sdk.TRANSCRIPT_DIRECTIVE_AREA}) contribution cc-env-card missing`)
    assert.equal(entry.id, 'cloud-compute:cc-env-card')
    assert.equal(entry.data?.name, 'cc-env')
    assert.equal(typeof entry.data?.render, 'function', 'data.render must be a function')
  })
)

await check('directive with an invalid id renders without ANY rest call', () =>
  withPlugin({}, async (ctx, fake) => {
    const entry = directive(fake)

    assert.ok(entry?.data?.render, 'directive cc-env-card is not registered')

    for (const id of ['', 'i-AB12CD34', 'i-ab12cd3', 'i-ab12cd345', 'ab12cd34', '../hosts', 'i-ab12cd34/../x', 'i-ab12cd3g', undefined]) {
      const attrs = id === undefined ? {} : { id }
      const element = entry.data.render({ attrs, sessionId: 's-1', streaming: false })

      if (element !== null && element !== undefined) {
        await renderUntilStable(element)
      }

      assert.deepEqual(fake.restCalls.map(c => `${c.method} ${c.path}`), [], `invalid id ${JSON.stringify(id)} triggered rest calls`)
    }
  })
)

await check('directive while streaming renders a placeholder with no fetch', () =>
  withPlugin({}, async (ctx, fake) => {
    const entry = directive(fake)

    assert.ok(entry?.data?.render, 'directive cc-env-card is not registered')

    const element = entry.data.render({ attrs: { id: 'i-ab12cd34' }, sessionId: null, streaming: true })

    if (element !== null && element !== undefined) {
      await renderUntilStable(element)
    }

    assert.deepEqual(fake.restCalls.map(c => `${c.method} ${c.path}`), [], 'streaming must not fetch')
  })
)

await check('directive with a valid id -> exactly GET /instances/i-ab12cd34 -> ChatCard (Screen hidden when unsupported)', () =>
  withPlugin({}, async (ctx, fake) => {
    const entry = directive(fake)

    assert.ok(entry?.data?.render, 'directive cc-env-card is not registered')

    let tree = await renderUntilStable(entry.data.render({ attrs: { id: 'i-ab12cd34' }, sessionId: 's-1', streaming: false }))

    assert.deepEqual(fake.restCalls.map(c => `${c.method} ${c.path}`), ['GET /instances/i-ab12cd34'])
    assert.ok(findAll(tree, n => n.name === 'ChatCard').length >= 1, 'a ChatCard is expected')

    const labels = findButtons(tree).labels()

    assert.ok(labels.includes('Terminal'), `ChatCard needs Terminal (have ${labels})`)
    assert.ok(!labels.includes('Screen'), 'Screen must be hidden for display_state "unsupported"')
    assert.ok(allText(tree).includes(PLAIN.name), 'ChatCard shows the instance name')

    tree = await click(findButtons(tree).byLabel('Terminal'))
    assert.ok(restLines(fake).includes('POST /instances/i-ab12cd34/terminal'), `Terminal -> POST .../terminal (calls: ${restLines(fake)})`)
  })
)

await check('ChatCard shows Screen for a display-capable instance and opens the viewer', () =>
  withPlugin({ 'GET /instances/{id}': () => DISPLAY }, async (ctx, fake) => {
    const entry = directive(fake)

    assert.ok(entry?.data?.render, 'directive cc-env-card is not registered')

    let tree = await renderUntilStable(entry.data.render({ attrs: { id: DISPLAY.id }, sessionId: null, streaming: false }))

    assert.ok(findButtons(tree).labels().includes('Screen'), `Screen expected (have ${findButtons(tree).labels()})`)
    hostFake.hostCalls.length = 0
    tree = await click(findButtons(tree).byLabel('Screen'))
    assert.ok(restLines(fake).includes(`POST /instances/${DISPLAY.id}/display`), `calls: ${restLines(fake)}`)
    assert.equal(callsOf('openWorkspace').length, 1)
  })
)

// ---- 9: overview tunnels ----------------------------------------------------------
await check('OverviewTab lists open tunnels (GET /tunnels) with a Close button -> DELETE /tunnels/{id}', () =>
  withPlugin({}, async (ctx, fake) => {
    const tunnels = readFixture('tunnels.json')
    let tree = await mountComponent(C.OverviewTab)

    assert.ok(fake.callsTo('GET', '/tunnels').length >= 1, 'OverviewTab must GET /tunnels')

    const close = findButtons(tree).allByLabel('Close')

    assert.equal(close.length, tunnels.filter(t => t.status === 'open').length, `one "Close" button per open tunnel (have ${findButtons(tree).labels()})`)
    tree = await click(close[0])

    const deletes = fake.restCalls.filter(c => c.method === 'DELETE' && c.pathname.startsWith('/tunnels/'))

    assert.equal(deletes.length, 1, `Close must DELETE /tunnels/{id} (calls: ${restLines(fake)})`)
    assert.ok(tunnels.some(t => deletes[0].pathname === `/tunnels/${t.id}`), `unexpected tunnel id in ${deletes[0].pathname}`)
  })
)

// =============================================================================
// First run, token masking, port forwarding and the Agents UI.
// =============================================================================
// TextField wraps an Input: drive the real <input>, which hands its event to the wrapper.
const inputField = (tree, keys, where) => {
  const node = findField(tree, keys, where)

  return findAll(node, n => n.type === 'input' || n.type === 'textarea')[0] ?? node
}
const TOKEN = '0123456789abcdef0123456789abcdef'
const TOKEN_URL = `http://127.0.0.1:41234/${TOKEN}/vnc_lite.html?path=${TOKEN}/websockify&scale=true`
const AGENTS = readFixture('agents.json')
const AGENT_REQUEST_KEYS = readFixture('agent_request_keys.json')
const agentById = status => AGENTS.find(a => a.status === status)
const callsMatching = (fake, method, pattern) => fake.restCalls.filter(c => c.method === method && pattern.test(c.pathname))
const cardOf = (tree, id) => findAll(tree, n => n.name === 'AgentRunCard' && n.props?.run?.id === id)[0]

// ---- secret tokens are never displayed ---------------------------------------
await check('maskUrl hides path tokens, path= tokens and passwords; plain addresses are untouched', () => {
  assert.equal(typeof T.maskUrl, 'function', '__test__.maskUrl is missing')

  const masked = T.maskUrl(TOKEN_URL)

  assert.ok(!masked.includes(TOKEN), masked)
  assert.equal(masked, 'http://127.0.0.1:41234/\u2022\u2022\u2022\u2022/vnc_lite.html?path=\u2022\u2022\u2022\u2022/websockify&scale=true')
  assert.equal(T.maskUrl('http://127.0.0.1:64082/'), 'http://127.0.0.1:64082/')
  assert.equal(T.maskUrl('http://127.0.0.1:18080/lab/tree'), 'http://127.0.0.1:18080/lab/tree')
  assert.equal(T.maskUrl('http://127.0.0.1:1/x?password=hunter2&a=1'), 'http://127.0.0.1:1/x?password=\u2022\u2022\u2022\u2022&a=1')
  assert.equal(T.maskUrl('http://example.org/abcdefghijklmnop'), 'http://example.org/\u2022\u2022\u2022\u2022', '16 characters is a token')
  assert.equal(T.maskUrl('http://example.org/abcdefghijklmno'.slice(0, -1)), 'http://example.org/abcdefghijklmn', '15 characters is not')
  assert.equal(T.maskUrl('http://averyveryverylonghostname.example/ok'), 'http://averyveryverylonghostname.example/ok', 'the host name is never masked')
  assert.equal(T.maskUrl(null), '')
  assert.equal(T.maskUrl('127.0.0.1:41234'), '127.0.0.1:41234')
  assert.equal(T.maskText(`Could not open ${TOKEN_URL}: refused`), 'Could not open http://127.0.0.1:41234/\u2022\u2022\u2022\u2022/vnc_lite.html?path=\u2022\u2022\u2022\u2022/websockify&scale=true: refused')
})

await check('the Open tunnels list shows masked addresses; Open in browser still uses the full one', () =>
  withPlugin({}, async (ctx, fake) => {
    const tunnels = readFixture('tunnels.json')
    const display = tunnels.find(t => t.kind === 'display')

    assert.equal(display.url, TOKEN_URL, 'the tunnels fixture should carry a token path')

    const tree = await mountComponent(C.OverviewTab)

    assert.ok(!allText(tree).includes(TOKEN), 'the secret token is visible in the Open tunnels list')
    assert.ok(allText(tree).includes('\u2022\u2022\u2022\u2022'), 'masked tokens should show as bullets')

    await click(findButtons(tree).byLabel('Open in browser'))
    assert.deepEqual(fake.osCalls.openExternal, [TOKEN_URL])
  })
)

await check('toasts are masked', () =>
  withPlugin({}, async (ctx, fake) => {
    hostFake.hostCalls.length = 0
    fake.emit('plugin.cloud-compute.notify', { level: 'info', title: 'Screen ready', body: `Open ${TOKEN_URL} now` })

    assert.ok(toastTexts().length >= 1)
    assert.ok(toastTexts().every(t => !t.includes(TOKEN)), `a toast shows the token: ${JSON.stringify(toastTexts())}`)
  })
)

await check('an action error that quotes a token address is masked', () =>
  withPlugin({ 'POST /instances/{id}/display': { status: 502, body: { error: { code: 'internal', message: `tunnel to ${TOKEN_URL} failed` } } } }, async () => {
    let tree = await mountComponent(C.InstanceCard, { instance: DISPLAY })

    tree = await click(findButtons(tree).byLabel('Screen'))
    assert.ok(allText(tree).includes('failed'), 'the error should show')
    assert.ok(!allText(tree).includes(TOKEN), 'the error shows the token')
  })
)

// ---- forward dialog repeats the typed port ------------------------------------
await check('forwarding shows the container port typed, the local URL, and a screen note for 6080', () =>
  withPlugin({ 'POST /instances/{id}/forward': { body: { ...readFixture('instance_forward.json'), local_port: 64082, remote_port: 37245, url: 'http://127.0.0.1:64082/' } } }, async (ctx, fake) => {
    let tree = await mountComponent(C.ForwardDialog, { instance: PLAIN, onClose() {} })

    tree = await setValue(inputField(tree, ['port inside the environment'], 'ForwardDialog'), '6080')
    tree = await click(findButtons(tree).byLabel('Forward'))

    const post = fake.restCalls.find(c => c.method === 'POST' && c.pathname === `/instances/${PLAIN.id}/forward`)

    assert.equal(bodyOf(post).remote_port, 6080)
    assert.ok(allText(tree).includes('Container port 6080 \u2192 http://127.0.0.1:64082/'), allText(tree))
    assert.ok(!allText(tree).includes('37245'), 'the published host port is not what the user typed')
    assert.ok(allText(tree).includes('Use Screen'), 'a forwarded display port should point to Screen')
  })
)

await check('an ordinary port has no screen note; Copy and Open use the full address', () =>
  withPlugin({ 'POST /instances/{id}/forward': { body: { ...readFixture('instance_forward.json'), url: TOKEN_URL } } }, async (ctx, fake) => {
    let tree = await mountComponent(C.ForwardDialog, { instance: PLAIN, onClose() {} })

    tree = await setValue(inputField(tree, ['port inside the environment'], 'ForwardDialog'), '8080')
    tree = await click(findButtons(tree).byLabel('Forward'))
    assert.ok(allText(tree).includes('Container port 8080 \u2192'))
    assert.ok(!allText(tree).includes('Use Screen'))
    assert.ok(!allText(tree).includes(TOKEN), 'the forward result shows the token')

    const copy = findAll(tree, n => n.name === 'CopyButton')[0]

    assert.equal(copy.props.text, TOKEN_URL, 'Copy must keep the full address')
    await click(findButtons(tree).byLabel('Open in browser'))
    assert.deepEqual(fake.osCalls.openExternal, [TOKEN_URL])
  })
)

// ---- first run -------------------------------------------------
const fillEndpoint = async (tree, alias) => setValue(inputField(tree, ['gpu-box', 'ssh alias'], 'wizard'), alias)

await check('before any host exists Help is reachable and the wizard keeps its state', () =>
  withPlugin({ 'GET /health': { fixture: 'health_unconfigured.json' } }, async () => {
    let tree = await mountComponent(C.Page)

    assert.ok(findAll(tree, n => n.name === 'AddHostWizard').length >= 1)

    tree = await selectTab(tree, 'help')
    assert.ok(allText(tree).includes('Host types'), 'Help content should show on the first-run page')
    assert.ok(findAll(tree, n => n.name === 'AddHostWizard').length >= 1, 'the wizard stays mounted behind Help')

    tree = await selectTab(tree, 'start')
    assert.ok(allText(tree).includes('SSH host'))
  })
)

await check('after Save the Host saved step stays up when /health flips to configured; Done leaves for the tabs', () => {
  let configured = false

  return withPlugin({ 'GET /health': () => ({ ...readFixture('health.json'), configured }) }, async (ctx, fake) => {
    let tree = await mountComponent(C.Page)

    assert.equal(findAll(tree, n => n.name === 'TabsTrigger').length, 2, 'first-run tabs expected')
    tree = await click(findButtons(tree).byLabel('Next'))
    tree = await fillEndpoint(tree, 'devbox')
    tree = await click(findButtons(tree).byLabel('Next'))
    tree = await click(findButtons(tree).byLabel('Save'))

    assert.equal(callsMatching(fake, 'POST', /^\/hosts$/).length, 1, `Save must POST /hosts (calls: ${restLines(fake)})`)
    assert.ok(allText(tree).includes('Host saved'), allText(tree))

    // The backend now has a host: the event refetches /health.
    configured = true
    fake.emit('plugin.cloud-compute.hosts.changed', { ids: ['devbox'] })
    tree = await tree.session.settle()

    assert.ok(allText(tree).includes('Host saved'), 'the wizard was unmounted when /health flipped')
    assert.equal(findButtons(tree).byLabel('Spawn first environment'), undefined, 'the wizard ends at Done')
    assert.equal(findButtons(tree).byLabel('Submit first job'), undefined)
    assert.ok(findButtons(tree).byLabel('Done'), 'one Done button')

    tree = await click(findButtons(tree).byLabel('Done'))
    assert.ok(findAll(tree, n => n.name === 'TabsTrigger').length >= 6, `the full tab bar should show after Done (have ${findAll(tree, n => n.name === 'TabsTrigger').length})`)
    assert.ok(!allText(tree).includes('Host saved'))
  })
})

await check('12/15: the done step is Host saved + status + one Done button + a pointer, and starts nothing', () => {
  let configured = false

  return withPlugin({ 'GET /health': () => ({ ...readFixture('health.json'), configured }) }, async (ctx, fake) => {
    let tree = await mountComponent(C.Page)

    tree = await click(findButtons(tree).byLabel('Next'))
    tree = await fillEndpoint(tree, 'devbox')
    tree = await click(findButtons(tree).byLabel('Next'))
    tree = await click(findButtons(tree).byLabel('Save'))
    configured = true

    const text = allText(tree)
    const done = findButtons(tree).filter(b => ['Done', 'Spawn first environment', 'Submit first job'].includes(b.label))

    assert.ok(text.includes('Host saved: '), text.slice(0, 200))
    assert.deepEqual(done.map(b => b.label), ['Done'], 'only a Done button ends the wizard')
    assert.ok(text.includes('Create an environment from the Templates tab whenever you need one.'), 'SSH pointer line')
    assert.ok(!text.includes('Submit a batch job'), 'no Jobs pointer on a container host')

    const before = fake.restCalls.length

    tree = await click(done[0])
    assert.equal(fake.restCalls.slice(before).filter(c => c.method === 'POST').length, 0, 'Done starts nothing')
    assert.ok(!allText(tree).includes('Spawn environment'), 'no spawn dialog opens after Done')
    assert.ok(findAll(tree, n => n.name === 'TabsTrigger').length >= 6, 'the tabs follow Done')
  })
})

await check('12/15: the done step for a Slurm host points to the Jobs tab and offers only Done', () =>
  withPlugin({ 'POST /hosts': { body: readFixture('hosts_slurm.json')[0] } }, async () => {
    let finished = null
    let tree = await mountComponent(C.AddHostWizard, { initial: { kind: 'slurm' }, onFinished: r => { finished = r } })

    tree = await click(findButtons(tree).byLabel('Next'))
    tree = await fillEndpoint(tree, 'login.example.org')
    tree = await click(findButtons(tree).byLabel('Next'))
    tree = await click(findButtons(tree).byLabel('Save'))

    assert.ok(allText(tree).includes('Host saved'))
    assert.equal(findButtons(tree).byLabel('Spawn first environment'), undefined, 'Slurm hosts cannot run environments')
    assert.equal(findButtons(tree).byLabel('Submit first job'), undefined, 'no first-job push')
    assert.ok(allText(tree).includes('Submit a batch job from the Jobs tab whenever you need one.'))
    assert.ok(!allText(tree).includes('Create an environment'))

    tree = await click(findButtons(tree).byLabel('Done'))
    assert.ok(finished?.host, 'Done reports the saved host')
    assert.equal(finished?.submit, undefined)
    assert.equal(finished?.spawn, undefined)
  })
)

await check('/health follows the backend: it refetches on an interval and on plugin events', () => {
  let configured = true

  return withPlugin({ 'GET /health': () => ({ ...readFixture('health.json'), configured }) }, async (ctx, fake) => {
    let tree = await mountComponent(C.Page)
    const health = getQueries().find(q => q.queryKey[3] === 'health')

    assert.ok(health, 'the health query is missing')
    assert.ok(health.refetchInterval > 0 && health.refetchInterval <= 30000, `health must refetch on an interval (got ${health.refetchInterval})`)
    assert.ok(findAll(tree, n => n.name === 'TabsTrigger').length >= 6)

    // A data reset: the page falls back to the wizard without a reload.
    configured = false
    fake.emit('plugin.cloud-compute.hosts.changed', {})
    tree = await tree.session.settle()
    assert.ok(allText(tree).includes('Connect your first machine'), 'the first-run view should return after a reset')
  })
})

await check('after Save -> Done -> tabs, a data reset (configured:false) returns the first-run view without a reload', () => {
  let configured = false

  return withPlugin({ 'GET /health': () => ({ ...readFixture('health.json'), configured }) }, async (ctx, fake) => {
    let tree = await mountComponent(C.Page)

    tree = await click(findButtons(tree).byLabel('Next'))
    tree = await fillEndpoint(tree, 'devbox')
    tree = await click(findButtons(tree).byLabel('Next'))
    tree = await click(findButtons(tree).byLabel('Save'))
    configured = true
    fake.emit('plugin.cloud-compute.hosts.changed', { ids: ['devbox'] })
    tree = await tree.session.settle()
    tree = await click(findButtons(tree).byLabel('Done'))
    assert.ok(findAll(tree, n => n.name === 'TabsTrigger').length >= 6, 'the tabs show after Done')

    configured = false
    fake.emit('plugin.cloud-compute.hosts.changed', {})
    tree = await tree.session.settle()
    assert.ok(allText(tree).includes('Connect your first machine'), `stale tabs after the reset: ${allText(tree).slice(0, 200)}`)
    assert.equal(findAll(tree, n => n.name === 'TabsTrigger').length, 2, 'the first-run tabs (Get started / Help) are back')

    // And the wizard works again: a new first host reaches Host saved, then the tabs.
    tree = await click(findButtons(tree).byLabel('Next'))
    tree = await fillEndpoint(tree, 'devbox2')
    tree = await click(findButtons(tree).byLabel('Next'))
    tree = await click(findButtons(tree).byLabel('Save'))
    configured = true
    fake.emit('plugin.cloud-compute.hosts.changed', { ids: ['devbox2'] })
    tree = await tree.session.settle()
    assert.ok(allText(tree).includes('Host saved'), allText(tree).slice(0, 200))
    tree = await click(findButtons(tree).byLabel('Done'))
    assert.ok(findAll(tree, n => n.name === 'TabsTrigger').length >= 6)
  })
})

await check('a stale health between Done and the refetch still reaches the tabs, and a later reset still returns to the wizard', () => {
  let configured = false

  return withPlugin({ 'GET /health': () => ({ ...readFixture('health.json'), configured }) }, async (ctx, fake) => {
    let tree = await mountComponent(C.Page)

    tree = await click(findButtons(tree).byLabel('Next'))
    tree = await fillEndpoint(tree, 'devbox')
    tree = await click(findButtons(tree).byLabel('Next'))
    tree = await click(findButtons(tree).byLabel('Save'))
    // The health refetch has NOT landed when Done is pressed.
    tree = await click(findButtons(tree).byLabel('Done'))
    assert.ok(findAll(tree, n => n.name === 'TabsTrigger').length >= 6, 'Done goes to the tabs even while /health is stale')

    configured = true
    fake.emit('plugin.cloud-compute.hosts.changed', {})
    tree = await tree.session.settle()
    assert.ok(findAll(tree, n => n.name === 'TabsTrigger').length >= 6)

    configured = false
    fake.emit('plugin.cloud-compute.hosts.changed', {})
    tree = await tree.session.settle()
    assert.ok(allText(tree).includes('Connect your first machine'), allText(tree).slice(0, 200))
  })
})

await check('after a reset no per-tab data stays cached or shown (stale Jobs list)', () => {
  let configured = true
  let reset = false

  return withPlugin(
    {
      'GET /health': () => ({ ...readFixture('health.json'), configured }),
      'GET /jobs': () => (reset ? [] : readFixture('jobs.json')),
      'GET /hosts': () => (reset ? [] : readFixture('hosts.json'))
    },
    async (ctx, fake) => {
      let tree = await mountComponent(C.Page)

      tree = await selectTab(tree, 'jobs')
      assert.ok(allText(tree).includes(readFixture('jobs.json')[0].name), 'the Jobs tab lists jobs before the reset')

      reset = true
      configured = false
      fake.emit('plugin.cloud-compute.hosts.changed', {})
      tree = await tree.session.settle()
      assert.ok(allText(tree).includes('Connect your first machine'), allText(tree).slice(0, 200))
      assert.ok(!allText(tree).includes(readFixture('jobs.json')[0].name), 'old jobs must not show on the first-run view')

      // The wizard's own host-types query is fresh static data; everything the tabs cached must be gone.
      const leftovers = getQueries().filter(q => !['health', 'host-types'].includes(q.queryKey[3])).map(q => q.queryKey[3])

      assert.deepEqual(leftovers, [], `cached per-tab queries survived the reset: ${leftovers}`)
    }
  )
})

// ---- the Agents tab -------------------------------------------------------------
await check('agentActions rules', () => {
  const names = status => T.agentActions({ status, instance_id: 'i-ab12cd34' })

  assert.deepEqual(names('running'), ['stop'])
  assert.deepEqual(names('starting'), ['stop'])
  assert.deepEqual(names('done'), ['collect', 'stop'])
  assert.deepEqual(names('failed'), ['stop'], 'a run that failed before it committed anything has nothing to get')
  assert.deepEqual(T.agentActions({ status: 'failed', instance_id: 'i-ab12cd34', commits_ahead: 2 }), ['collect', 'stop'], 'a failed run with commits can be collected')
  assert.deepEqual(names('collected'), ['collect', 'stop'])
  assert.deepEqual(names('stopped'), [])
})

await check('AgentsTab lists runs with status, host, goal, branch, commits ahead, times and the log tail; refreshes every 5 s', () =>
  withPlugin({}, async (ctx, fake) => {
    const tree = await mountComponent(C.AgentsTab)
    const text = allText(tree)

    assert.ok(fake.callsTo('GET', '/agents').length >= 1)
    assert.equal(findAll(tree, n => n.name === 'AgentRunCard').length, AGENTS.length)

    const running = agentById('running')
    const card = textOf(cardOf(tree, running.id))

    for (const part of [running.id, 'running', running.goal, `Host ${running.host_id}`, `Branch ${running.branch}`, '1 commit ahead', 'Started']) {
      assert.ok(card.includes(part), `the run card is missing "${part}": ${card}`)
    }

    assert.ok(card.includes(running.last_log.at(-1)), 'the log tail should show')
    assert.ok(findAll(cardOf(tree, running.id), n => n.name === 'LogView').length === 1, 'the log tail uses LogView')
    assert.ok(textOf(cardOf(tree, agentById('done').id)).includes('Ended'))
    assert.ok(text.includes(agentById('failed').error), 'a failed run shows its error')
    assert.ok(findAll(cardOf(tree, agentById('collected').id), n => n.name === 'CollectInfo').length === 1, 'a collected run shows where it went')

    const query = getQueries().find(q => q.queryKey[3] === 'agents')

    assert.equal(query?.refetchInterval, 5000)
    assert.equal(findButtons(tree).allByLabel('Get results').length, 1, 'a done run can be collected; the failed one has no commits and the collected one has no sandbox left')
    assert.equal(findButtons(tree).allByLabel('Stop').length, 3, 'running, done and failed runs can be stopped; collected/stopped ones have no sandbox')
  })
)

await check('AgentsTab empty state explains cloud agents in one sentence', () =>
  withPlugin({ 'GET /agents': { body: [] } }, async () => {
    const tree = await mountComponent(C.AgentsTab)

    assert.equal(findAll(tree, n => n.name === 'EmptyState').length, 1)
    assert.ok(allText(tree).includes('A cloud agent is a Hermes agent'), allText(tree))
    assert.ok(findButtons(tree).byLabel('New agent run…'))
  })
)

await check('Collect POSTs /agents/{id}/collect and shows the ref, worktree, divergence and dry-run conflicts', () =>
  withPlugin({}, async (ctx, fake) => {
    const done = agentById('done')
    const result = readFixture('agent_collect.json')
    let tree = await mountComponent(C.AgentRunCard, { run: done })

    tree = await click(findButtons(tree).byLabel('Get results'))

    const post = callsMatching(fake, 'POST', /^\/agents\/[^/]+\/collect$/)

    assert.equal(post.length, 1, restLines(fake).join(', '))
    assert.equal(post[0].pathname, `/agents/${done.id}/collect`)
    assert.deepEqual(bodyOf(post[0]), { into: 'worktree' })

    const text = allText(tree)

    assert.ok(text.includes(result.ref), text)
    assert.ok(text.includes(result.worktree_path), text)
    assert.ok(text.includes('Diverged'), text)
    assert.ok(text.includes('README.md'), 'the dry-run conflicts show')
    assert.ok(notifyMessages().some(m => String(m).includes(result.ref)), 'a toast confirms the collect')
  })
)

await check('Stop asks for confirmation, then POSTs /agents/{id}/stop', () =>
  withPlugin({}, async (ctx, fake) => {
    const running = agentById('running')
    let tree = await mountComponent(C.AgentRunCard, { run: running })

    tree = await click(findButtons(tree).byLabel('Stop'))
    assert.ok(openConfirm(tree), 'Stop must open a ConfirmDialog')
    assert.equal(callsMatching(fake, 'POST', /\/stop$/).length, 0, 'nothing is stopped before the user confirms')

    tree = await click(findButtons(openConfirm(tree)).byLabel('Stop'))

    const post = callsMatching(fake, 'POST', /\/stop$/)

    assert.equal(post.length, 1)
    assert.equal(post[0].pathname, `/agents/${running.id}/stop`)
    assert.deepEqual(bodyOf(post[0]), { keep_branch: true })
  })
)

await check('a collect conflict from the API shows the message', () =>
  withPlugin({ 'POST /agents/{id}/collect': { status: 409, body: { error: { code: 'conflict', message: 'agent run a-1a2b3c4d is still running; wait for it or stop it first' } } } }, async () => {
    let tree = await mountComponent(C.AgentRunCard, { run: agentById('done') })

    tree = await click(findButtons(tree).byLabel('Get results'))
    assert.ok(allText(tree).includes('still running; wait for it or stop it first'), allText(tree))
  })
)

await check('a diverged Collect shows the core message and the incoming ref it kept', () =>
  withPlugin({ 'POST /agents/{id}/collect': { body: readFixture('agent_collect_diverged.json') } }, async () => {
    const result = readFixture('agent_collect_diverged.json')
    let tree = await mountComponent(C.AgentRunCard, { run: agentById('done') })

    tree = await click(findButtons(tree).byLabel('Get results'))

    const text = allText(tree)

    assert.ok(text.includes('Diverged'), text)
    assert.ok(text.includes(result.message), 'the message from the core is shown whole: ' + text)
    assert.ok(text.includes('Kept as'), text)

    const kept = findAll(tree, n => n.name === 'CollectInfo').length
    const incoming = result.message.match(/refs\/cloud-compute\/incoming\/\S+(?=\s)/)[0]

    assert.equal(kept, 1)
    assert.ok(incoming.endsWith('/cloud/write-readme'), incoming)
    assert.ok(allText(tree).split(incoming).length >= 3, 'the incoming ref also gets its own row')
  })
)

await check('a diverged Collect toasts a warning that says so and names the kept ref, not a plain success', () =>
  withPlugin({ 'POST /agents/{id}/collect': { body: readFixture('agent_collect_diverged.json') } }, async () => {
    const result = readFixture('agent_collect_diverged.json')
    let tree = await mountComponent(C.AgentRunCard, { run: agentById('done') })

    tree = await click(findButtons(tree).byLabel('Get results'))

    const toasts = callsOf('notify').map(c => c.args[0])
    const incoming = result.message.match(/refs\/cloud-compute\/incoming\/\S+(?=\s)/)[0]

    assert.equal(toasts.length, 1, JSON.stringify(toasts))
    assert.equal(toasts[0].kind, 'warning', JSON.stringify(toasts[0]))
    assert.ok(toasts[0].message.includes('diverged'), toasts[0].message)
    assert.ok(toasts[0].message.includes(incoming), toasts[0].message)
    assert.ok(!toasts[0].message.startsWith('Collected '), toasts[0].message)
  })
)

await check('a clean Collect keeps the success toast; a blocked one (message, not diverged) is a warning', async () => {
  await withPlugin({ 'POST /agents/{id}/collect': { body: { ...readFixture('agent_collect.json'), diverged: false, conflicts_dry_run: [] } } }, async () => {
    const tree = await mountComponent(C.AgentRunCard, { run: agentById('done') })

    await click(findButtons(tree).byLabel('Get results'))

    const toasts = callsOf('notify').map(c => c.args[0])

    assert.equal(toasts.length, 1)
    assert.equal(toasts[0].kind, 'success')
    assert.ok(toasts[0].message.startsWith('Collected '), toasts[0].message)
  })

  await withPlugin({ 'POST /agents/{id}/collect': { body: { ...readFixture('agent_collect_diverged.json'), status: 'blocked', diverged: false, message: 'The branch is checked out in /home/me/demo and is dirty. Commit or stash there, then Collect again.' } } }, async () => {
    const tree = await mountComponent(C.AgentRunCard, { run: agentById('done') })

    await click(findButtons(tree).byLabel('Get results'))

    const toasts = callsOf('notify').map(c => c.args[0])

    assert.equal(toasts[0].kind, 'warning')
    assert.ok(toasts[0].message.includes('Commit or stash'), toasts[0].message)
  })
})

await check('a blocked Collect shows the message; no message and no status add nothing', () =>
  withPlugin({ 'POST /agents/{id}/collect': { body: { ...readFixture('agent_collect_diverged.json'), status: 'blocked', diverged: false, message: '/home/me/demo has the branch checked out and could not be fast-forwarded (dirty). Commit or stash the changes there, then Collect again.' } } }, async () => {
    let tree = await mountComponent(C.AgentRunCard, { run: agentById('done') })

    tree = await click(findButtons(tree).byLabel('Get results'))

    const text = allText(tree)

    assert.ok(text.includes('Blocked'), text)
    assert.ok(text.includes('Commit or stash the changes there'), text)
    assert.ok(!text.includes('Diverged'), text)
  })
)

await check('a clean Collect shows no warning, no message and no Kept as row', () =>
  withPlugin({}, async () => {
    let tree = await mountComponent(C.AgentRunCard, { run: agentById('done') })

    tree = await click(findButtons(tree).byLabel('Get results'))

    const text = allText(tree)

    assert.ok(!text.includes('Blocked') && !text.includes('Kept as') && !text.includes('nothing was changed'), text)
  })
)

await check('template cards say "from Settings" for an unset limit, and keep explicit values', () =>
  withPlugin({}, async () => {
    const tree = await mountComponent(C.TemplatesTab)
    const text = allText(tree)

    assert.ok(text.includes('time limit from Settings') && text.includes('idle stop from Settings'), text)
    assert.ok(text.includes('time limit 480 min'), 'an explicit template value still shows')
    assert.ok(text.includes('no time limit'), 'a Slurm job template keeps "no time limit"')
  })
)

await check('the template editor says a blank limit means the Settings default (jobs: none)', () =>
  withPlugin({}, async () => {
    const [container] = readFixture('templates.json').filter(t => t.kind === 'container')
    const job = readFixture('templates.json').find(t => t.kind === 'slurm_job')
    const a = allText(await mountComponent(C.TemplateEditor, { template: container, onClose: () => {} }))
    const b = allText(await mountComponent(C.TemplateEditor, { template: job, onClose: () => {} }))

    assert.ok(a.includes('Time limit (minutes, blank = Settings default)') && a.includes('Idle timeout (minutes, blank = Settings default)'), a)
    assert.ok(b.includes('Time limit (minutes, blank = none)'), b)
  })
)

await check('New agent run… posts a spawn request with the core keys and the documented defaults', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.AgentsTab)

    tree = await click(findButtons(tree).byLabel('New agent run…'))

    const dialog = () => openDialog(tree, 'NewAgentDialog')

    assert.ok(dialog(), 'New agent run… must open NewAgentDialog')

    // Start with nothing typed: refused locally, no request.
    tree = await click(findButtons(dialog()).byLabel('Start'))
    assert.equal(callsMatching(fake, 'POST', /^\/agents$/).length, 0)
    assert.ok(allText(dialog()).includes('Describe what the agent should do.'))

    tree = await setValue(inputField(dialog(), ['task for the agent'], 'NewAgentDialog'), 'Add a --verbose flag')
    tree = await setValue(inputField(dialog(), ['repository path on this computer'], 'NewAgentDialog'), '/home/dev/code/demo')
    tree = await click(findButtons(dialog()).byLabel('Start'))

    const posts = callsMatching(fake, 'POST', /^\/agents$/)

    assert.equal(posts.length, 1, restLines(fake).join(', '))

    const body = bodyOf(posts[0])

    assert.ok(Object.keys(body).every(k => AGENT_REQUEST_KEYS.includes(k)), `unexpected keys in ${JSON.stringify(body)}`)
    assert.deepEqual(body, {
      goal: 'Add a --verbose flag',
      host_id: 'x1carbon',
      repo_path: '/home/dev/code/demo',
      new_branch: true,
      template: 'hermes-agent',
      max_turns: 30,
      base: 'HEAD'
    })
    assert.ok(!openDialog(tree, 'NewAgentDialog'), 'the dialog closes after a successful start')
  })
)

await check('the host picker only offers hosts that allow agents; none -> explained, Start disabled', () =>
  withPlugin({ 'GET /hosts': { body: readFixture('hosts.json').map(h => ({ ...h, spec: { ...h.spec, limits: { ...h.spec.limits, allow_agents: false } } })) } }, async () => {
    let tree = await mountComponent(C.AgentsTab)

    tree = await click(findButtons(tree).byLabel('New agent run…'))
    assert.ok(allText(tree).includes('No host allows cloud agents'), allText(tree))
    assert.equal(findButtons(openDialog(tree, 'NewAgentDialog')).byLabel('Start').props.disabled, true)
  })
)

await check('a missing model setup error is shown with a link to Settings', () =>
  withPlugin({ 'POST /agents': { status: 400, body: { error: { code: 'invalid', message: 'Set the cloud agent model endpoint and model in Settings \u2192 Own Compute (or in the template\'s Agent section).' } } } }, async (ctx, fake) => {
    let tree = await mountComponent(C.AgentsTab)

    tree = await click(findButtons(tree).byLabel('New agent run…'))
    tree = await setValue(inputField(openDialog(tree, 'NewAgentDialog'), ['task for the agent'], 'NewAgentDialog'), 'Do a thing')
    tree = await setValue(inputField(openDialog(tree, 'NewAgentDialog'), ['repository path on this computer'], 'NewAgentDialog'), '/repo')
    tree = await click(findButtons(openDialog(tree, 'NewAgentDialog')).byLabel('Start'))

    assert.ok(allText(tree).includes('Set the cloud agent model endpoint and model in Settings'), allText(tree))
    assert.ok(openDialog(tree, 'NewAgentDialog'), 'the dialog stays open on an error')

    const open = findButtons(tree).byLabel('Open settings')

    assert.ok(open, `an Open settings button expected (have ${findButtons(tree).labels()})`)
    hostFake.hostCalls.length = 0
    await click(open)
    assert.deepEqual(callsOf('navigate').map(c => c.args[0]), ['#/settings?plugin=cloud-compute'])
  })
)

await check('the palette entry opens the New agent run dialog on the Agents tab', () =>
  withPlugin({}, async (ctx, fake) => {
    const entry = fake.find(sdk.PALETTE_AREA, 'new-agent')

    assert.ok(entry, 'palette entry new-agent missing')
    assert.equal(entry.data.label, 'New cloud agent run…')

    hostFake.hostCalls.length = 0
    await entry.data.run()
    assert.deepEqual(callsOf('navigate').map(c => c.args[0]), ['/cloud-compute'])

    const tree = await mountComponent(C.AgentsTab)

    assert.ok(openDialog(tree, 'NewAgentDialog'), 'the Agents tab should open the dialog the palette asked for')
  })
)

await check('cloud_agent_* tools and agents.changed events invalidate the page queries', () =>
  withPlugin({}, async (ctx, fake) => {
    for (const name of ['cloud_agent_spawn', 'cloud_agent_status', 'cloud_agent_collect', 'cloud_agent_stop']) {
      assert.equal(T.shouldInvalidate('tool.complete', { name }), true, name)
    }

    queryClient.reset()
    fake.emit('plugin.cloud-compute.agents.changed', { ids: ['a-1a2b3c4d'], reason: 'done' })
    assert.ok(invalidatedOurs())
  })
)

// ---- the template Agent section ----------------------------------------------------
const hermesTemplate = readFixture('templates.json').find(t => t.id === 'hermes-agent')
const plainTemplate = readFixture('templates.json').find(t => !t.agent)

await check('the template editor shows the Agent section: mode, model, base URL and the key line', () =>
  withPlugin({}, async () => {
    const tree = await mountComponent(C.TemplateEditor, { template: hermesTemplate, onClose() {} })
    const text = allText(tree)

    assert.ok(text.includes('Agent'), text)
    assert.ok(text.includes('Mode: hermes'), text)
    assert.ok(text.includes('Model key: CC_AGENT_API_KEY (set in your Hermes profile .env)'), text)
    assert.ok(text.includes('Blank uses the default from Settings.'), text)
    inputField(tree, ['model'], 'TemplateEditor')
    inputField(tree, ['model base url'], 'TemplateEditor')
  })
)

await check('a template without an agent object has no Agent section', () =>
  withPlugin({}, async () => {
    const tree = await mountComponent(C.TemplateEditor, { template: plainTemplate, onClose() {} })

    assert.ok(!allText(tree).includes('Model key'))
    assert.ok(!allText(tree).includes('Mode:'))
  })
)

await check('saving sends the whole agent object with the edited model and base URL; blank means null', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.TemplateEditor, { template: hermesTemplate, onClose() {} })

    tree = await setValue(inputField(tree, ['model'], 'TemplateEditor'), 'qwen3-coder')
    tree = await setValue(inputField(tree, ['model base url'], 'TemplateEditor'), 'http://127.0.0.1:8000/v1')
    tree = await click(findButtons(tree).byLabel('Save'))

    let patch = callsMatching(fake, 'PATCH', /^\/templates\/hermes-agent$/)

    assert.equal(patch.length, 1, restLines(fake).join(', '))
    assert.deepEqual(bodyOf(patch[0]).agent, { ...hermesTemplate.agent, model: 'qwen3-coder', base_url: 'http://127.0.0.1:8000/v1' })

    // Blank fields go back to the Settings defaults.
    tree = await setValue(inputField(tree, ['model'], 'TemplateEditor'), '  ')
    tree = await setValue(inputField(tree, ['model base url'], 'TemplateEditor'), '')
    tree = await click(findButtons(tree).byLabel('Save'))
    patch = callsMatching(fake, 'PATCH', /^\/templates\/hermes-agent$/)
    assert.deepEqual(bodyOf(patch.at(-1)).agent, { ...hermesTemplate.agent, model: null, base_url: null })
  })
)

await check('a templates without an agent object sends no agent key', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.TemplateEditor, { template: plainTemplate, onClose() {} })

    tree = await click(findButtons(tree).byLabel('Save'))

    const patch = callsMatching(fake, 'PATCH', /^\/templates\//)

    assert.equal(patch.length, 1)
    assert.ok(!('agent' in bodyOf(patch[0])), 'agent must not be sent for a plain template')
  })
)

await check('buildAgentRequest validates and shapes the spawn body', () => {
  const ok = { goal: ' g ', host_id: 'h', repo_path: ' /r ', mode: 'new', branch: '', base: 'main', template: 'agent-script', max_turns: '5' }

  assert.deepEqual(T.buildAgentRequest(ok).body, { goal: 'g', host_id: 'h', repo_path: '/r', new_branch: true, template: 'agent-script', max_turns: 5, base: 'main' })
  assert.deepEqual(T.buildAgentRequest({ ...ok, mode: 'existing', branch: 'dev' }).body, { goal: 'g', host_id: 'h', repo_path: '/r', new_branch: false, template: 'agent-script', max_turns: 5, branch: 'dev' })
  assert.ok(T.buildAgentRequest({ ...ok, goal: ' ' }).error)
  assert.ok(T.buildAgentRequest({ ...ok, host_id: '' }).error)
  assert.ok(T.buildAgentRequest({ ...ok, repo_path: '' }).error)
  assert.ok(T.buildAgentRequest({ ...ok, max_turns: '0' }).error)
  assert.ok(T.buildAgentRequest({ ...ok, max_turns: '2.5' }).error)
  assert.equal(T.buildAgentRequest({ ...ok, max_turns: '' }).body.max_turns, 30)
})

await check('every API_CALLS entry is well-formed', () => {
  const seen = new Set()

  for (const call of T.API_CALLS) {
    assert.ok(/^(GET|POST|PATCH|PUT|DELETE)$/.test(call.method), `bad method ${call.method}`)
    assert.ok(typeof call.path === 'string' && call.path.startsWith('/'), `bad path ${call.path}`)
    assert.ok(!seen.has(`${call.method} ${call.path}`), `duplicate ${call.method} ${call.path}`)
    seen.add(`${call.method} ${call.path}`)
  }
})

// ---- Slurm polish: allocated meters, stale jobs, unconfirmed ends ---------------------
// The fixtures behind these checks are compared with the real handlers in
// tests/test_desktop_fixtures.py (list_hosts, _mark_stale, SlurmManager job.finished).
const slurmHosts = readFixture('hosts_slurm.json')
const slurmCard = slurmHosts.find(h => h.state && h.state.resources)
const plainCard = readFixture('hosts.json').find(h => h.state && h.state.resources && h.state.resources.source !== 'slurm')

await check('polish: Slurm host meters read CPU allocated and RAM allocated', () =>
  withPlugin({}, async () => {
    const text = allText(await mountComponent(C.HostCard, { item: slurmCard }))

    assert.equal(slurmCard.state.resources.basis, 'allocated')
    assert.ok(text.includes('CPU allocated'), `missing "CPU allocated": ${text.slice(0, 300)}`)
    assert.ok(text.includes('RAM allocated'), `missing "RAM allocated": ${text.slice(0, 300)}`)
  })
)

await check('polish: hosts without basis keep the plain CPU and RAM labels', () =>
  withPlugin({}, async () => {
    const text = allText(await mountComponent(C.HostCard, { item: plainCard }))

    assert.equal(plainCard.state.resources.basis, undefined)
    assert.ok(text.includes('CPU') && text.includes('RAM'), 'plain meters missing')
    assert.ok(!text.includes('allocated'), 'a non-Slurm host must not say "allocated"')
  })
)

// EC2 hosts (fixture tests/fixtures/desktop/host_ec2.json).
const ec2Hosts = readFixture('host_ec2.json')

// The card asks for the machine and the instances found on AWS when it shows.
const ec2Machine = (untracked = []) =>
  Object.fromEntries(ec2Hosts.map(h => [`GET /hosts/${h.spec.id}/machine`, { body: { machine: {}, untracked: { untracked } } }]))

await check('ec2: a running machine shows its type, the billing sentence and Stop/Extend/Terminate', () =>
  withPlugin(ec2Machine(), async () => {
    const item = JSON.parse(JSON.stringify(ec2Hosts.find(h => h.spec.ec2.machine_state === 'ready')))
    item.spec.ec2.auto_stop_at = new Date(Date.now() + 3600000).toISOString()
    const text = allText(await mountComponent(C.HostCard, { item }))
    assert.ok(text.includes('Machine: Running'), text.slice(0, 400))
    assert.ok(text.includes('t3.micro in us-east-1, 10 GB disk'))
    assert.ok(text.includes('AWS bills your account while the machine runs'))
    for (const b of ['Stop machine', 'Extend 1 h', 'Terminate machine', 'Check AWS']) assert.ok(text.includes(b), b)
    assert.ok(!text.includes('Start machine'))
  })
)

const ec2Calls = (fake, method, pathname) => fake.restCalls.filter(c => c.method === method && c.pathname === pathname)

await check('ec2: Start, Extend and Stop ask first with the backend text and act only on the confirm button', () => {
  const cases = [
    ['stopped', 'Start machine', 'start', 'Starts the stopped EC2 machine i-0bbb. Start number 2. Starts billing now: yes.'],
    ['ready', 'Extend 1 h', 'extend', 'It now stops itself at 12:00; after this it stops itself at 13:00.'],
    ['ready', 'Stop machine', 'stop', 'The disk is still billed until the machine is terminated.']
  ]
  return (async () => {
    for (const [state, label, action, message] of cases) {
      await withPlugin({
    'GET /hosts/aws-x/machine': { body: { machine: {}, untracked: { untracked: [] } } },
    [`GET /hosts/aws-x/machine/${action}/confirm`]: { body: { action, message, blocked: false } },
    [`POST /hosts/aws-x/machine/${action}`]: { body: { machine: {}, untracked: { untracked: [] } } }
  }, async (ctx, fake) => {
    const item = JSON.parse(JSON.stringify(ec2Hosts.find(h => h.spec.ec2.machine_state === state)))
    item.spec.id = 'aws-x'
    item.spec.ec2.auto_stop_at = new Date(Date.now() + 3600000).toISOString()
    let tree = await mountComponent(C.HostCard, { item })
    tree = await click(findButtons(tree).byLabel(label))
    assert.deepEqual(ec2Calls(fake, 'POST', `/hosts/aws-x/machine/${action}`), [], `${action}: nothing happens on the first click`)
    assert.equal(ec2Calls(fake, 'GET', `/hosts/aws-x/machine/${action}/confirm`).length, 1)
    assert.ok(allText(tree).includes(message), `${action}: the backend text is shown`)
    assert.ok(findButtons(tree).byLabel('Cancel'), `${action}: Cancel is offered`)
    tree = await click(findButtons(tree).allByLabel(label).pop())
    assert.equal(ec2Calls(fake, 'POST', `/hosts/aws-x/machine/${action}`).length, 1, `${action}: confirmed`)
      })
    }
  })()
})

await check('ec2: a refused action shows its reason and offers only Close', () =>
  withPlugin({
    'GET /hosts/aws-x/machine': { body: { machine: {}, untracked: { untracked: [] } } },
    'GET /hosts/aws-x/machine/extend/confirm': { body: { action: 'extend', message: 'It cannot be extended beyond 2026-11-11 14:00 UTC.', blocked: true } }
  }, async (ctx, fake) => {
    const item = JSON.parse(JSON.stringify(ec2Hosts.find(h => h.spec.ec2.machine_state === 'ready')))
    item.spec.id = 'aws-x'
    item.spec.ec2.auto_stop_at = new Date(Date.now() + 3600000).toISOString()
    let tree = await mountComponent(C.HostCard, { item })
    tree = await click(findButtons(tree).byLabel('Extend 1 h'))
    assert.ok(allText(tree).includes('cannot be extended beyond'))
    assert.ok(findButtons(tree).byLabel('Close'))
    assert.equal(findButtons(tree).allByLabel('Extend 1 h').length, 1, 'only the card button; no confirm button')
    assert.deepEqual(ec2Calls(fake, 'POST', '/hosts/aws-x/machine/extend'), [])
  })
)

await check('ec2: in the last minute before its stop time the card does not offer Extend and says why', () =>
  withPlugin(ec2Machine(), async () => {
    const make = ms => {
      const item = JSON.parse(JSON.stringify(ec2Hosts.find(h => h.spec.ec2.machine_state === 'ready')))
      item.spec.ec2.auto_stop_at = new Date(Date.now() + ms).toISOString()
      return item
    }
    const late = allText(await mountComponent(C.HostCard, { item: make(30000) }))
    assert.ok(!late.includes('Extend 1 h'), 'no Extend 30 s before the stop')
    assert.ok(late.includes('about to stop at its time limit'), late.slice(0, 500))
    assert.ok(late.includes('Stop machine') && late.includes('Terminate machine'))
    const early = allText(await mountComponent(C.HostCard, { item: make(10 * 60000) }))
    assert.ok(early.includes('Extend 1 h') && !early.includes('about to stop'))
  })
)

await check('ec2: Extend offers the step the backend says fits, and plain text when none does', () =>
  withPlugin({
    'GET /hosts/aws-x/machine': { body: { machine: {}, untracked: { untracked: [] } } },
    'GET /hosts/aws-x/machine/extend/confirm': { body: { action: 'extend', message: 'Extends it.', blocked: false } }
  }, async (ctx, fake) => {
    const make = step => {
      const item = JSON.parse(JSON.stringify(ec2Hosts.find(h => h.spec.ec2.machine_state === 'ready')))
      item.spec.id = 'aws-x'
      item.spec.ec2.auto_stop_at = new Date(Date.now() + 10 * 60000).toISOString()
      item.spec.ec2.extend_step_minutes = step
      return item
    }
    let tree = await mountComponent(C.HostCard, { item: make(15) })
    assert.ok(allText(tree).includes('Extend 15 min') && !allText(tree).includes('Extend 1 h'), allText(tree).slice(0, 500))
    tree = await click(findButtons(tree).byLabel('Extend 15 min'))
    assert.equal(fake.restCalls.filter(c => c.method === 'GET' && c.pathname === '/hosts/aws-x/machine/extend/confirm')[0].query.minutes, '15')
    const none = allText(await mountComponent(C.HostCard, { item: make(0) }))
    assert.ok(!/Extend \d/.test(none), 'no Extend button when no step fits')
    assert.ok(none.includes('Already at the longest run time for this host. Change Max lifetime on the host to allow more.'), none.slice(0, 500))
    assert.ok(none.includes('Stop machine') && none.includes('Terminate machine'))
    const hour = allText(await mountComponent(C.HostCard, { item: make(60) }))
    assert.ok(hour.includes('Extend 1 h') && !hour.includes('Already at the longest'))
  })
)

await check('ec2: a stopped machine offers Start, says the disk is kept, and shows no billing line', () =>
  withPlugin(ec2Machine(), async () => {
    const item = ec2Hosts.find(h => h.spec.ec2.machine_state === 'stopped')
    const text = allText(await mountComponent(C.HostCard, { item }))
    assert.ok(text.includes('Machine: Stopped') && text.includes('Start machine'))
    assert.ok(text.includes('the disk is kept'))
    assert.ok(!text.includes('Stop machine'))
  })
)

await check('ec2: no machine and a stopped machine are shown neutrally: no offline wording, no red error, no network hint', () =>
  withPlugin(ec2Machine(), async () => {
    const stopped = ec2Hosts.find(h => h.spec.ec2.machine_state === 'stopped')
    for (const [machine_state, label] of [['stopped', 'Machine stopped'], ['none', 'No machine yet']]) {
      const item = JSON.parse(JSON.stringify(stopped))
      item.spec.ec2.machine_state = machine_state
      item.state.last_error = machine_state === 'none' ? 'No machine: press Start machine to launch one.' : 'The machine is stopped. Stopped: the disk is kept.'
      const text = allText(await mountComponent(C.HostCard, { item }))
      assert.ok(text.includes(label), machine_state + ': ' + text.slice(0, 300))
      for (const bad of ['Last error', 'on your network', 'Offline', 'offline', 'No answer on port', 'no-machine.invalid', 'Last seen', 'What to do']) assert.ok(!text.includes(bad), machine_state + ' must not show ' + bad + ': ' + text.slice(0, 500))
      assert.ok(text.includes('Start machine'))
    }
  })
)

await check('ec2: a machine in error still shows its error, with a hint that fits an EC2 host', () =>
  withPlugin(ec2Machine(), async () => {
    const item = JSON.parse(JSON.stringify(ec2Hosts.find(h => h.spec.ec2.machine_state === 'stopped')))
    item.spec.ec2.machine_state = 'error'
    item.spec.ec2.machine_detail = 'The machine did not become ready.'
    item.state.last_error = 'The machine did not become ready.'
    const text = allText(await mountComponent(C.HostCard, { item }))
    assert.ok(text.includes('Last error: The machine did not become ready.'))
    assert.ok(!text.includes('on your network'))
    assert.ok(text.includes('What to do: open the machine panel'))
  })
)

await check('ec2: a machine that is starting, launching or in error with an instance offers Stop and Terminate', () =>
  withPlugin(ec2Machine(), async () => {
    const base = ec2Hosts.find(h => h.spec.ec2.machine_state === 'ready')
    for (const machine_state of ['launching', 'starting', 'error']) {
      const item = JSON.parse(JSON.stringify(base))
      item.spec.ec2.machine_state = machine_state
      const text = allText(await mountComponent(C.HostCard, { item }))
      assert.ok(text.includes('Stop machine') && text.includes('Terminate machine'), machine_state)
    }
  })
)

await check('ec2: the card says how long the machine runs if Hermes closes early, only when its boot timer differs', () =>
  withPlugin(ec2Machine(), async () => {
    const base = ec2Hosts.find(h => h.spec.ec2.machine_state === 'stopped')
    const same = JSON.parse(JSON.stringify(base))
    same.spec.ec2.unit_lifetime_minutes = same.spec.ec2.max_lifetime_minutes
    assert.ok(!allText(await mountComponent(C.HostCard, { item: same })).includes('If Hermes closes'))
    const old = JSON.parse(JSON.stringify(base))
    old.spec.ec2.max_lifetime_minutes = 15
    old.spec.ec2.unit_lifetime_minutes = 1440
    const text = allText(await mountComponent(C.HostCard, { item: old }))
    assert.ok(text.includes('If Hermes closes before it reaches the machine, the machine stops after 24 h instead.'), text.slice(0, 600))
  })
)

await check('ec2: the wizard asks region, type, image, lifetime and idle stop, and names the cost', () =>
  withPlugin(ec2Machine(), async () => {
    const initial = ec2Hosts[0].spec
    const text = allText(await mountComponent(C.AddHostWizard, { initial, onClose() {}, onFinished() {} }))
    for (const f of ['Region', 'Instance type', 'Image', 'Root disk (GB)', 'Max lifetime (minutes)', 'Stop when idle', 'Who may use SSH', 'Show free-tier eligible types']) assert.ok(text.includes(f), f)
    assert.ok(text.includes('never the root user'))
    assert.ok(text.includes('A whole number from 15 to 1440') && text.includes('A whole number from 8 to 500'), 'the allowed ranges are stated next to the fields')
    assert.ok(text.includes('AWS bills your account while the machine runs'))
    assert.ok(!text.includes('SSH alias'), 'no SSH alias field for EC2')
    assert.ok(!/\bis free\b/i.test(text), 'never claims anything is free')
  })
)

const sshHosts = readFixture('hosts.json')
const shippedTemplates = readFixture('templates.json')
await check('ec2: a template for SSH hosts is offered on an EC2 host (the rule of the spawn check)', () => {
  const ready = ec2Hosts.find(h => h.spec.ec2.machine_state === 'ready')
  const ssh = sshHosts.find(h => h.spec.kind === 'ssh')
  const hosts = [ssh, ready]
  assert.deepEqual(T.eligibleHosts({ host_kinds: ['ssh', 'local'] }, hosts).map(h => h.spec.id), [ssh.spec.id, ready.spec.id])
  assert.deepEqual(T.eligibleHosts({ host_kinds: ['local'] }, hosts), [], 'a local-only template does not fit an EC2 host')
  assert.deepEqual(T.eligibleHosts({ host_kinds: ['slurm'] }, hosts), [])
})

await check('ec2: the Spawn dialog lists an EC2 host and says what a machine that is not running needs', () =>
  withPlugin({}, async () => {
    const base = ec2Hosts.find(h => h.spec.ec2.machine_state === 'ready')
    const make = (machine_state, auto_start) => {
      const item = JSON.parse(JSON.stringify(base))
      item.spec.ec2.machine_state = machine_state
      item.spec.ec2.auto_start = auto_start
      return item
    }
    assert.equal(T.spawnMachineNote(base).text, '')
    assert.deepEqual(T.spawnMachineNote(make('stopped', false)), { blocked: true, text: 'machine stopped: start it on the Hosts tab' })
    assert.deepEqual(T.spawnMachineNote(make('none', false)), { blocked: true, text: 'no machine yet: start it on the Hosts tab' })
    assert.deepEqual(T.spawnMachineNote(make('stopped', true)), { blocked: false, text: 'starts the machine first' })
    assert.equal(T.spawnMachineNote(sshHosts[0]).text, '')
    const stopped = make('stopped', false)
    const tree = await mountComponent(C.SpawnDialog, { templates: shippedTemplates, hosts: [stopped], template: shippedTemplates.find(t => t.id === 'python-dev'), hostId: stopped.spec.id, onClose() {}, onSpawned() {} })
    const text = allText(tree)
    assert.ok(!text.includes('No host of a matching type'), text)
    assert.ok(text.includes('This host cannot be used right now: machine stopped: start it on the Hosts tab.'), text)
    assert.equal(findButtons(tree).byLabel('Spawn').props.disabled, true)
  })
)

await check('ec2: instances found on AWS that no host tracks are listed at once, with a warning', () =>
  withPlugin(ec2Machine([{ instance_id: 'i-0abc0abc0abc0abc0', state: 'running', instance_type: 'c7i.large' }]), async () => {
    const item = ec2Hosts.find(h => h.spec.ec2.machine_state === 'ready')
    const text = allText(await mountComponent(C.HostCard, { item }))
    assert.ok(text.includes('i-0abc0abc0abc0abc0'), text.slice(0, 600))
    assert.ok(text.includes('AWS bills the account for these until they are stopped or terminated'))
  })
)

await check('polish: unknown memory used shows RAM n/a, never 0', () =>
  withPlugin({}, async () => {
    const [item] = readFixture('hosts_slurm_mem_unknown.json')

    assert.equal(item.state.resources.mem_used_mb, null)

    const text = allText(await mountComponent(C.HostCard, { item }))

    assert.ok(text.includes('RAM n/a'), `missing "RAM n/a": ${text.slice(0, 300)}`)
    assert.ok(!text.includes('RAM allocated'), 'unknown memory must not be drawn as an allocated meter')
    assert.ok(!/\b0 MB\b/.test(text) && !text.includes('0 / '), 'unknown memory must not read as 0')

    const meters = T.hostMeters(item.state.resources)

    assert.deepEqual(meters.map(m => m.label), ['CPU allocated', 'RAM n/a'])
    assert.equal(T.hostMeters(null)[1].label, 'RAM', 'no resources at all: plain RAM label, shown as unknown')
  })
)

await check('polish: jobs flagged stale get a muted "not refreshed" hint; others do not', () =>
  withPlugin({ 'GET /jobs': { body: readFixture('jobs_stale.json') } }, async () => {
    const stale = readFixture('jobs_stale.json')
    const tree = await mountComponent(C.JobsTab)
    const names = stale.map(j => j.name)

    for (const job of stale) {
      const row = rowOf(tree, job.name, names.filter(n => n !== job.name))

      assert.ok(row, `no row for ${job.name}`)
      assert.equal(textOf(row).includes('not refreshed'), job.stale === true, `${job.name}: "not refreshed" hint should ${job.stale ? '' : 'not '}be shown`)
    }
  })
)

await check('polish: plain jobs (no stale flag) never show the hint', () =>
  withPlugin({}, async () => {
    assert.ok(!allText(await mountComponent(C.JobsTab)).includes('not refreshed'))
  })
)

await check('polish: an UNKNOWN job with ended_at reads "Ended (state unconfirmed)"; open UNKNOWN stays UNKNOWN', () =>
  withPlugin({}, async () => {
    const unconfirmed = jobsFixture.find(j => j.state === 'UNKNOWN' && j.ended_at)

    assert.ok(unconfirmed, 'jobs.json needs an unconfirmed finished job')
    assert.equal(T.jobStateLabel(unconfirmed), 'Ended (state unconfirmed)')
    assert.equal(T.jobStateLabel({ state: 'UNKNOWN', ended_at: null }), 'UNKNOWN')
    assert.equal(T.jobStateLabel({ state: 'FAILED', ended_at: '2026-11-08T08:03:02Z' }), 'FAILED')
    assert.equal(T.jobStateLabel({}), 'UNKNOWN')

    const names = jobsFixture.map(j => j.name)
    const row = rowOf(await mountComponent(C.JobsTab), unconfirmed.name, names.filter(n => n !== unconfirmed.name))

    assert.ok(row && textOf(row).includes('Ended (state unconfirmed)'), 'JobCard badge should say "Ended (state unconfirmed)"')
    assert.deepEqual(T.jobActions(unconfirmed), ['output', 'delete'], 'a finished job offers Output and Remove from list')

    const open = textOf(rowOf(await mountComponent(C.JobsTab), RUNNING_JOB.name, names.filter(n => n !== RUNNING_JOB.name)))

    assert.ok(!open.includes('unconfirmed'))
  })
)

await check('polish: job.finished with confirmed:false says the final state could not be confirmed', () =>
  withPlugin({}, async (ctx, fake) => {
    queryClient.reset()
    hostFake.hostCalls.length = 0
    fake.osCalls.notify.length = 0
    fake.emit('plugin.cloud-compute.job.finished', readFixture('job_finished_unconfirmed.json'))

    const toasts = toastTexts()

    assert.ok(toasts.some(t => t.includes('legacy-run') && t.toLowerCase().includes('could not be confirmed')), `toast: ${JSON.stringify(toasts)}`)
    assert.ok(!toasts.some(t => t.includes('finished: UNKNOWN')), 'must not claim a plain "finished: UNKNOWN"')
    assert.ok(fake.osCalls.notify.length >= 1, 'ctx.os.notify was not called')
    assert.ok(invalidatedOurs(), 'job.finished must still invalidate [\"cloud-compute\"]')
  })
)

await check('polish: a confirmed job.finished keeps the "finished: STATE" wording', () =>
  withPlugin({}, async (ctx, fake) => {
    queryClient.reset()
    hostFake.hostCalls.length = 0
    fake.emit('plugin.cloud-compute.job.finished', { id: 'j-99aa11bb', host_id: 'slurm-x1', slurm_job_id: '41', name: 'preprocess', state: 'COMPLETED', exit_code: '0:0' })

    assert.ok(toastTexts().some(t => t.includes('preprocess finished: COMPLETED')))
  })
)

// ---- first host, the POST reply loses the race with the /health refetch ------------
const flush = async () => {
  for (let i = 0; i < 4; i += 1) await new Promise(resolve => setImmediate(resolve))
}

await check('/health flips to configured BEFORE the POST /hosts reply; the wizard survives and shows Host saved', () => {
  let configured = false
  let release
  const gate = new Promise(resolve => { release = resolve })

  return withPlugin(
    {
      'GET /health': () => ({ ...readFixture('health.json'), configured }),
      'POST /hosts': async () => {
        await gate
        configured = true

        return readFixture('host_created.json')
      }
    },
    async (ctx, fake) => {
      let tree = await mountComponent(C.Page)

      tree = await click(findButtons(tree).byLabel('Next'))
      tree = await fillEndpoint(tree, 'devbox')
      tree = await click(findButtons(tree).byLabel('Next'))
      tree = await click(findButtons(tree).byLabel('Save'))
      assert.equal(callsMatching(fake, 'POST', /^\/hosts$/).length, 1, 'Save must POST /hosts')

      // The backend has stored the host and announced it; the reply is still on its way.
      configured = true
      const healthBefore = fake.callsTo('GET', '/health').length
      fake.emit('plugin.cloud-compute.hosts.changed', { ids: ['devbox'] })
      tree = await tree.session.settle()
      assert.ok(fake.callsTo('GET', '/health').length > healthBefore, 'the event must refetch /health')

      // The reply lands, then another refetch.
      release()
      await flush()
      tree = await tree.session.settle()
      fake.emit('plugin.cloud-compute.hosts.changed', { ids: ['devbox'] })
      tree = await tree.session.settle()

      assert.ok(allText(tree).includes('Host saved'), `the saved step was lost: ${allText(tree).slice(0, 300)}`)
      assert.equal(findButtons(tree).byLabel('Spawn first environment'), undefined, 'the wizard ends at Done')
      assert.ok(findButtons(tree).byLabel('Done'), 'Done is offered')
      assert.equal(findAll(tree, n => n.name === 'TabsTrigger').length, 2, 'still the first-run tabs until Done')

      tree = await click(findButtons(tree).byLabel('Done'))
      assert.ok(findAll(tree, n => n.name === 'TabsTrigger').length >= 6, 'the full tabs after Done')
      assert.ok(!allText(tree).includes('Host saved'))
    }
  )
})

await check('the same, for a Slurm host: the saved step survives the early /health flip', () => {
  let configured = false
  let release
  const gate = new Promise(resolve => { release = resolve })

  return withPlugin(
    {
      'GET /health': () => ({ ...readFixture('health.json'), configured }),
      'POST /hosts': async () => {
        await gate

        return readFixture('hosts_slurm.json')[0]
      }
    },
    async (ctx, fake) => {
      let tree = await mountComponent(C.Page)

      tree = await click(findButtons(tree).byLabel('Next'))
      tree = await fillEndpoint(tree, 'devbox')
      tree = await click(findButtons(tree).byLabel('Next'))
      tree = await click(findButtons(tree).byLabel('Save'))
      configured = true
      fake.emit('plugin.cloud-compute.hosts.changed', {})
      tree = await tree.session.settle()
      release()
      await flush()
      tree = await tree.session.settle()

      assert.ok(allText(tree).includes('Host saved'), allText(tree).slice(0, 300))
    }
  )
})

await check('a failed first Save releases the pin: the form shows the error, later the tabs follow /health', () => {
  let configured = false

  return withPlugin(
    {
      'GET /health': () => ({ ...readFixture('health.json'), configured }),
      'POST /hosts': { status: 409, body: { error: { code: 'conflict', message: 'host devbox already exists' } } }
    },
    async (ctx, fake) => {
      let tree = await mountComponent(C.Page)

      tree = await click(findButtons(tree).byLabel('Next'))
      tree = await fillEndpoint(tree, 'devbox')
      tree = await click(findButtons(tree).byLabel('Next'))
      tree = await click(findButtons(tree).byLabel('Save'))
      assert.ok(allText(tree).includes('host devbox already exists'), allText(tree).slice(0, 300))
      assert.ok(!allText(tree).includes('Host saved'))

      configured = true
      fake.emit('plugin.cloud-compute.hosts.changed', {})
      tree = await tree.session.settle()
      assert.ok(findAll(tree, n => n.name === 'TabsTrigger').length >= 6, 'a failed Save must not keep the wizard up for good')
    }
  )
})

// ---- actions that cannot apply -----------------------------------------------------
await check('agentActions needs the sandbox: gone -> nothing; stopped sandbox -> Collect still works', () => {
  const live = [{ id: 'i-ab12cd34', status: 'running' }]
  const idle = [{ id: 'i-ab12cd34', status: 'stopped' }]

  assert.deepEqual(T.agentActions({ status: 'collected', instance_id: 'i-ab12cd34' }, live), ['collect', 'stop'])
  assert.deepEqual(T.agentActions({ status: 'done', instance_id: 'i-ab12cd34' }, idle), ['collect', 'stop'], 'an idle-stopped sandbox is restarted by Collect')
  assert.deepEqual(T.agentActions({ status: 'collected', instance_id: 'i-ab12cd34' }, []), [], 'sandbox destroyed after a collect (Stop keeps status collected)')
  assert.deepEqual(T.agentActions({ status: 'done', instance_id: 'i-ab12cd34' }, []), [])
  assert.deepEqual(T.agentActions({ status: 'failed', instance_id: '' }, live), [])
  assert.deepEqual(T.agentActions({ status: 'running', instance_id: 'i-ab12cd34' }, []), ['stop'], 'a live run can always be stopped')
  assert.deepEqual(T.agentActions({ status: 'starting', instance_id: '' }, []), ['stop'])
  assert.deepEqual(T.agentActions({ status: 'collected', instance_id: 'i-ab12cd34' }), ['collect', 'stop'], 'instance list not loaded yet: trust the run')
})

await check('after Stop the Agents tab drops Collect and Stop on a collected run whose sandbox was destroyed', () => {
  const runs = AGENTS.map(a => (a.status === 'collected' ? { ...a, instance_id: 'i-ab12cd34' } : a))
  const noSandbox = instancesFixture.filter(i => i.id !== 'i-ab12cd34')

  return withPlugin({ 'GET /agents': { body: runs }, 'GET /instances': { body: noSandbox } }, async () => {
    const tree = await mountComponent(C.AgentsTab)
    const collected = cardOf(tree, agentById('collected').id)

    assert.equal(findButtons(collected).byLabel('Get results'), undefined)
    assert.equal(findButtons(collected).byLabel('Stop'), undefined)
    // The run whose sandbox still exists keeps its buttons.
    assert.ok(findButtons(cardOf(tree, agentById('done').id)).byLabel('Get results'))
    assert.ok(findButtons(cardOf(tree, agentById('running').id)).byLabel('Stop'))
  })
})

// ---- one error sentence ----------------------------------------------------------------
await check('the no-model error shows the backend sentence once, plus Open settings and Edit templates', () => {
  const sentence = "Set the cloud agent model endpoint and model in Settings \u2192 Own Compute (or in the template's Agent section)."

  return withPlugin({ 'POST /agents': { status: 400, body: { error: { code: 'invalid', message: sentence } } } }, async () => {
    let tree = await mountComponent(C.AgentsTab)

    tree = await click(findButtons(tree).byLabel('New agent run…'))
    tree = await setValue(inputField(openDialog(tree, 'NewAgentDialog'), ['task for the agent'], 'NewAgentDialog'), 'Do a thing')
    tree = await setValue(inputField(openDialog(tree, 'NewAgentDialog'), ['repository path on this computer'], 'NewAgentDialog'), '/repo')
    tree = await click(findButtons(openDialog(tree, 'NewAgentDialog')).byLabel('Start'))

    const text = allText(tree)

    assert.equal(text.split(sentence).length - 1, 1, `the backend sentence must show once: ${text.slice(-400)}`)
    assert.ok(!text.includes('Set the model endpoint and model in Settings'), 'no second, near-identical sentence')
    assert.ok(findButtons(tree).byLabel('Open settings'))
    assert.ok(findButtons(tree).byLabel('Edit templates'))
  })
})

// ---- the Screen tab follows its environment -------------------------------------------------
const stoppedInst = id => ({ ...instancesFixture.find(i => i.id === id), status: 'stopped' })

await check('instance stopped -> host.closeWorkspace closes the Screen tab when this desktop has it', () =>
  withPlugin({ 'GET /instances/{id}': { body: stoppedInst(DISPLAY.id) } }, async (ctx, fake) => {
    T.openScreens.clear()
    host.closeWorkspace = (...args) => hostFake.hostCalls.push({ method: 'closeWorkspace', args })

    try {
      hostFake.hostCalls.length = 0
      await T.openScreen(DISPLAY)
      fake.emit('plugin.cloud-compute.instances.changed', { reason: 'stopped' })
      await flush()

      assert.deepEqual(callsOf('closeWorkspace').map(c => c.args[0]), [`cc-display-${DISPLAY.id}`])
    } finally {
      delete host.closeWorkspace
    }
  })
)

await check('without host.closeWorkspace the tab says the screen ended and offers Close; a running environment is left alone', () => {
  let status = 'running'

  return withPlugin({ 'GET /instances/{id}': () => ({ ...instancesFixture.find(i => i.id === DISPLAY.id), status }) }, async (ctx, fake) => {
    T.openScreens.clear()
    hostFake.hostCalls.length = 0
    await T.openScreen(DISPLAY)

    const [opened] = callsOf('openWorkspace')
    let tree = await renderUntilStable(opened.args[1].render())

    assert.equal(findAll(tree, n => n.name === 'SandboxedFrame').length, 1)

    // Running: an event changes nothing.
    fake.emit('plugin.cloud-compute.instances.changed', {})
    await flush()
    tree = await tree.session.settle()
    assert.equal(findAll(tree, n => n.name === 'SandboxedFrame').length, 1, 'no change while the environment runs')

    // Stopped: the black frame goes, the message and Close show.
    status = 'stopped'
    fake.emit('plugin.cloud-compute.tunnels.changed', { ids: ['t'] })
    await flush()
    tree = await tree.session.settle()

    assert.equal(findAll(tree, n => n.name === 'SandboxedFrame').length, 0, 'the dead frame must go')
    assert.ok(allText(tree).includes('Screen ended (environment stopped)'), allText(tree))
    assert.equal(findButtons(tree).byLabel('Stop screen').props.disabled, true)

    tree = await click(findButtons(tree).byLabel('Close'))
    assert.deepEqual(callsOf('openWorkspace.close').map(c => c.args[0]), [`cc-display-${DISPLAY.id}`])
  })
})

await check('the open Screen tab also checks on its own interval (a missed event)', () =>
  withPlugin({ 'GET /instances/{id}': { status: 404, body: { error: { code: 'not_found', message: 'gone' } } } }, async (ctx, fake) => {
    T.openScreens.clear()
    hostFake.hostCalls.length = 0
    await T.openScreen(DISPLAY)

    const [opened] = callsOf('openWorkspace')
    let tree = await renderUntilStable(opened.args[1].render())
    const timer = fake.intervals.find(i => i.ms === 15000 && !i.cancelled)

    assert.ok(timer, 'ScreenViewer must poll through ctx.setInterval')
    await timer.fn()
    await flush()
    tree = await tree.session.settle()
    assert.ok(allText(tree).includes('Screen ended (environment stopped)'), 'a destroyed environment (404) ends the screen')
  })
)

// =============================================================================
// Help layout, wording, host buttons, capability notes,
// grouped hosts, terminal feedback. Each check names the feedback row it covers.
// =============================================================================
const hostsFixture = readFixture('hosts.json')
const kindOfHost = id => hostsFixture.find(h => h.spec.id === id).spec.kind
const toggleOf = (tree, titlePart) => findButtons(tree).find(b => b.label.includes(titlePart))
const cardCount = tree => findAll(tree, n => n.name === 'HostCard').length

await check('Help text lists sit inside the text margin (outside markers, left padding, nested, code blocks)', () => {
  const css = T.DOC_CSS

  assert.equal(typeof css, 'string', '__test__.DOC_CSS is missing')
  assert.match(css, /\.cc-doc\.cc-doc ul, \.cc-doc\.cc-doc ol \{[^}]*padding-left: 1\.5rem[^}]*list-style-position: outside/)
  assert.match(css, /ul \{ list-style-type: disc/)
  assert.match(css, /ol \{ list-style-type: decimal/)
  assert.match(css, /li ul, \.cc-doc\.cc-doc li ol \{[^}]*padding-left: 1\.25rem/, 'nested lists indent further')
  assert.match(css, /pre \{[^}]*overflow-x: auto[^}]*\}/, 'code blocks scroll instead of spilling out')
  assert.match(css, /h3 \{[^}]*font-weight: 600/, 'headings are styled as headings')
  assert.ok(!/list-style-position: inside/.test(css), 'never inside: that pushes bullets past the margin')
  assert.ok(!/#[0-9a-f]{3,6}\b|rgb\(/i.test(css), 'theme variables only')
})

await check('rows 1/2/6: topic dialog wraps the text in the list-fixing class and labels external links under Related links', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.HelpTab)
    const row = findButtons(tree).find(b => b.label.startsWith('SSH hosts') && b.label.includes('Read'))

    assert.ok(row, `a topic row for SSH hosts (have ${labelsOf(tree).slice(0, 12)})`)
    tree = await click(row)

    const wrapper = findAll(tree, n => typeof n.props?.className === 'string' && n.props.className.split(' ').includes('cc-doc'))[0]
    const style = findAll(tree, n => n.type === 'style')[0]

    assert.ok(wrapper, 'the rendered Help text sits in a .cc-doc wrapper')
    assert.ok(style && textOf(style).includes('list-style-position: outside'), 'the list styles ship with the dialog')

    const text = allText(tree)

    assert.ok(text.includes('Related links'), 'external links are under a Related links heading')
    assert.ok(text.includes('These open in your browser.'))

    const links = readFixture('docs_topic.json').links
    const buttons = findButtons(tree)

    for (const link of links) {
      assert.ok(buttons.byLabel(link.label + ' \u2197'), `link "${link.label}" is labelled as an external link`)
    }

    await click(buttons.byLabel(links[0].label + ' \u2197'))
    assert.deepEqual(fake.osCalls.openExternal, [links[0].url])
  })
)

await check('topics are one clickable row each, the action sits next to the name, and the heading says it is the plugin guide', () =>
  withPlugin({}, async () => {
    const tree = await mountComponent(C.HelpTab)
    const text = allText(tree)
    const topics = readFixture('docs_list.json')
    const rows = findAll(tree, n => n.type === 'button' && n.props?.type === 'button' && textOf(n).includes('Read'))

    assert.ok(text.includes('Own Compute guide'), 'section heading')
    assert.ok(!/(^|[^a-z])Topics($|[^a-z])/.test(text), 'the vague "Topics" heading is gone')
    assert.equal(rows.length, topics.length, 'one clickable row per topic')

    for (const row of rows) {
      assert.ok(!row.props.className.includes('justify-between'), 'the action must not be pushed to the far edge')
      assert.ok(row.props.className.includes('w-full'), 'the whole row is the click target')
    }

    assert.equal(findButtons(tree).allByLabel('Learn more').length, 4, 'Learn more is only on the four host type cards')
    assert.ok(topics.some(t => t.title === 'Using Own Compute from chat'))
    assert.ok(text.includes('Using Own Compute from chat') && !text.includes('Using it from chat'))
  })
)

await check('the offline topic list also uses the clearer chat title', () =>
  withPlugin({ 'GET /docs': { status: 404 } }, async () => {
    const text = allText(await mountComponent(C.HelpTab))

    assert.ok(text.includes('Using Own Compute from chat'))
    assert.ok(!text.includes('Using it from chat'))
  })
)

await check('Refresh status and Test connection carry tooltips that say what each does', () =>
  withPlugin({}, async (ctx, fake) => {
    const [first] = hostsFixture
    let tree = await mountComponent(C.HostCard, { item: first })
    const labels = labelsOf(tree)

    assert.ok(labels.includes('Refresh status') && labels.includes('Test connection'))
    assert.ok(!labels.includes('Probe now') && !labels.includes('Test'), `old names are gone (have ${labels})`)

    const tips = findAll(tree, n => n.name === 'Tip').map(n => n.props.label)

    assert.ok(tips.some(t => t === T.COPY.tooltips.refresh_status && /Re-reads/.test(t) && /resources and capabilities/.test(t)))
    assert.ok(tips.some(t => t === T.COPY.tooltips.test_connection && /checklist/.test(t) && /Nothing is saved/.test(t)))

    tree = await click(findButtons(tree).byLabel('Test connection'))
    assert.ok(fake.restCalls.some(c => c.method === 'POST' && c.pathname === '/hosts/test'), 'Test connection -> POST /hosts/test')
    assert.ok(allText(tree).includes('Connection test: ' + first.spec.name))
  })
)

await check('capability chips drop "(partial)"; plain conditions are listed per capability and host type', () =>
  withPlugin({}, async () => {
    const text = allText(await mountComponent(C.AddHostWizard, { onClose() {}, onFinished() {} }))

    assert.ok(!/partial/i.test(text))
    assert.ok(text.includes('GPU: if your cluster has GPUs.'), 'Slurm GPU condition')
    assert.ok(text.includes('Screen: needs the display image on the host; the Screen button offers to build it.'), 'SSH screen condition')
    assert.ok(text.includes('Costs money: only if your site bills for cluster time.'), 'Slurm cost condition')
    assert.ok(text.includes('Costs money: billed by AWS while the machine runs, and a little for its disk while it is stopped.'), 'EC2 cost condition')
    assert.ok(text.includes('Cloud agents: off until allowed in the host\'s sandbox defaults.'))
  })
)

await check('without /host-types the embedded notes still show', () =>
  withPlugin({ 'GET /host-types': { status: 404 } }, async () => {
    const text = allText(await mountComponent(C.HelpTab))

    assert.ok(text.includes('GPU: if your cluster has GPUs.'))
    assert.ok(!/partial/i.test(text))
    assert.deepEqual(T.capNotes({ kind: 'slurm' }), T.COPY.type_cards.slurm.capability_notes)
    assert.deepEqual(T.capNotes({ kind: 'nope' }), {})
    assert.deepEqual(T.capNotes({ kind: 'slurm', capability_notes: { gpu: 'x' } }), { gpu: 'x' }, 'the server note wins')
  })
)

await check('rows 14/Slurm fields: every Slurm advanced field has a one-line help text; the agents switch explains itself', () =>
  withPlugin({}, async () => {
    let tree = await mountComponent(C.AddHostWizard, { initial: { kind: 'slurm' }, onFinished() {} })

    tree = await click(findButtons(tree).byLabel('Next'))

    const hints = T.COPY.wizard_hints
    let text = allText(tree)

    for (const key of ['jump_host', 'partition', 'account', 'qos', 'workdir']) {
      assert.ok(text.includes(hints[key]), `help text for ${key} is shown under its field`)
    }

    assert.ok(hints.workdir.includes('not a sandbox'), 'the work directory text answers the sandbox question')

    tree = await click(findButtons(tree).byLabel('Advanced'))
    text = allText(tree)
    assert.ok(text.includes(hints.exec_prefix), 'command prefix help')
    assert.ok(text.includes(hints.interactive_prefix), 'interactive command prefix help')

    tree = await fillEndpoint(tree, 'login.example.org')
    tree = await click(findButtons(tree).byLabel('Next'))
    text = allText(tree)
    assert.ok(text.includes(hints.slurm_agents), 'plain reason for no cloud agents on Slurm')
    assert.ok(!/site policy/i.test(text), 'no unexplained "site policy"')
  })
)

await check('hosts are grouped by type in collapsible sections; collapse state is kept in the tab', () =>
  withPlugin({}, async () => {
    let tree = await mountComponent(C.HostsTab)
    const kinds = hostsFixture.map(h => h.spec.kind)
    const count = k => kinds.filter(x => x === k).length

    for (const [title, kind] of [['SSH hosts', 'ssh'], ['Slurm login nodes', 'slurm'], ['Local Docker/Podman', 'local']]) {
      assert.ok(toggleOf(tree, title + ' (' + count(kind) + ')'), `section "${title}" with its count`)
    }

    assert.equal(cardCount(tree), hostsFixture.length, 'all hosts show while expanded')

    // headings come in a fixed order: SSH, Slurm, Local
    const order = findAll(tree, n => n.type === 'section' && n.props['data-group']).map(n => n.props['data-group'])
    assert.deepEqual(order, ['ssh', 'slurm', 'local'])

    tree = await click(toggleOf(tree, 'Slurm login nodes ('))
    assert.equal(cardCount(tree), hostsFixture.length - count('slurm'), 'collapsing Slurm hides only its hosts')
    assert.equal(toggleOf(tree, 'Slurm login nodes (').props['aria-expanded'], false)
    assert.ok(toggleOf(tree, 'SSH hosts (').props['aria-expanded'])

    // a refetch keeps the choice
    tree = await tree.session.settle()
    assert.equal(cardCount(tree), hostsFixture.length - count('slurm'))

    tree = await click(toggleOf(tree, 'Slurm login nodes ('))
    assert.equal(cardCount(tree), hostsFixture.length, 'expanding shows them again')
  })
)

await check('groupHosts orders known types first and puts unknown kinds last', () => {
  const mk = kind => ({ spec: { id: kind + '-1', kind } })
  const groups = T.groupHosts([mk('local'), mk('zeta'), mk('slurm'), mk('ssh'), mk('ssh')])

  assert.deepEqual(groups.map(g => g.kind), ['ssh', 'slurm', 'local', 'zeta'])
  assert.equal(groups[0].items.length, 2)
  assert.deepEqual(T.groupHosts([]), [])
  assert.deepEqual(T.groupHosts(null), [])
})

await check('Terminal success shows a toast naming the environment; repeat clicks are ignored for 3 s', () =>
  withPlugin({}, async (ctx, fake) => {
    hostFake.hostCalls.length = 0
    let tree = await mountComponent(C.InstanceCard, { instance: DISPLAY })

    tree = await click(findButtons(tree).byLabel('Terminal'))
    assert.deepEqual(notifyMessages(), [`Terminal opened for ${DISPLAY.name}. It can appear behind this window.`])

    const button = findButtons(tree).byLabel('Terminal')

    assert.equal(button.props.disabled, true, 'the button is disabled for a moment')
    const cooldown = fake.timeouts.find(t => t.ms === 3000)
    assert.ok(cooldown, 'the cooldown runs through ctx.setTimeout (3000 ms)')

    // a click that gets through anyway (second card, chat card) is dropped by the guard
    const again = await T.openTerminal({ kind: 'instance', id: DISPLAY.id, name: DISPLAY.name })
    assert.equal(again, null, 'a repeat open within 3 s does nothing')
    assert.equal(fake.restCalls.filter(c => c.pathname.endsWith('/terminal')).length, 1, 'only one terminal request')

    cooldown.fn()
    tree = await tree.session.settle()
    assert.notEqual(findButtons(tree).byLabel('Terminal').props.disabled, true, 'the button comes back')

    T.resetTerminalGuard()
    assert.ok(await T.openTerminal({ kind: 'instance', id: DISPLAY.id, name: DISPLAY.name }), 'opens again once the window has passed')
    assert.equal(fake.restCalls.filter(c => c.pathname.endsWith('/terminal')).length, 2)
  })
)

await check('Open shell on a Slurm host gives the same feedback, and a failed launch can be retried at once', () =>
  withPlugin({}, async (ctx, fake) => {
    hostFake.hostCalls.length = 0
    const slurm = hostsFixture.find(h => h.spec.kind === 'slurm' && h.state?.status === 'online')
    let tree = await mountComponent(C.HostCard, { item: slurm })

    tree = await click(findButtons(tree).byLabel('Open shell'))
    assert.deepEqual(notifyMessages(), [`Terminal opened for ${slurm.spec.name}. It can appear behind this window.`])
    assert.equal(findButtons(tree).byLabel('Open shell').props.disabled, true)
    assert.ok(findAll(tree, n => n.name === 'Tip').some(n => /login node/.test(n.props.label)), 'tooltip says what Open shell opens')
  })
)

await check('a launch that failed (copy fallback) does not lock the button out', () =>
  withPlugin({ 'POST /instances/{id}/terminal': { fixture: 'instance_terminal_unlaunched.json' } }, async (ctx, fake) => {
    assert.ok(await T.openTerminal({ kind: 'instance', id: 'i-ab12cd34' }))
    assert.ok(await T.openTerminal({ kind: 'instance', id: 'i-ab12cd34' }), 'retry right away')
    assert.equal(fake.restCalls.filter(c => c.pathname.endsWith('/terminal')).length, 2)
    assert.ok(!notifyMessages().some(m => m.startsWith('Terminal opened')), 'no success toast for a copy fallback')
  })
)

await check('capability wording: an environment whose template has no screen says so plainly', () =>
  withPlugin({}, async () => {
    const plain = allText(await mountComponent(C.InstanceCard, { instance: PLAIN }))
    const screen = allText(await mountComponent(C.InstanceCard, { instance: DISPLAY }))

    assert.equal(PLAIN.display, false)
    assert.ok(plain.includes('Screen: not enabled in this template'))
    assert.ok(!/unsupported/i.test(plain))
    assert.ok(!screen.includes('not enabled in this template'))
  })
)

await check('Help copy in plugin.js is platform-neutral', () => {
  const copy = JSON.stringify(T.COPY)

  assert.ok(!/Windows Terminal inside/.test(copy))
  assert.ok(copy.includes('Windows Terminal or Command Prompt on Windows, Terminal on macOS, your usual terminal on Linux'))
  assert.ok(!/\bPC\b/.test(copy))
  assert.ok(!/University/.test(copy))
})

// =============================================================================
// Data safety, Files dialog and saved copies.
// =============================================================================
const { restError } = await import('./fake-ctx.mjs')
const minutesFromNow = min => new Date(Date.now() + min * 60000).toISOString().replace(/\.\d+Z$/, 'Z')
const EXPORTS = readFixture('exports.json')
const DESTROYED_FILES_SENTENCE = 'The files in /work (8 files, 1.9 MB) were saved to '
const FETCHED = readFixture('export_record.json')
const FILES = readFixture('files_list.json')
const callsTo = (fake, method, pathname) => fake.restCalls.filter(c => c.method === method && c.pathname === pathname)
const buttonsOf = tree => findButtons(tree).labels()

await check('stop reasons: every reason has its own plain text, and running ones have none', () => {
  const base = { ...STOPPED, status: 'stopped', host_id: 'x1carbon' }

  assert.equal(T.stopReasonText({ ...base, stop_reason: 'expired' }), 'Stopped: time limit reached. Files are kept.')
  assert.equal(T.stopReasonText({ ...base, stop_reason: 'idle' }), 'Stopped after being idle. Files are kept.')
  assert.equal(T.stopReasonText({ ...base, stop_reason: 'orphan' }), 'Found on x1carbon without a record. Files are kept.')
  assert.equal(T.stopReasonText({ ...base, stop_reason: 'user' }), 'Stopped.')
  assert.match(T.stopReasonText({ ...base, stop_reason: 'host-gone' }), /host.*(removed|reached).*not touched/)
  assert.equal(T.stopReasonText({ ...PLAIN, status: 'running' }), null)
})

await check('InstanceCard: a stopped environment shows why it stopped; expired ones say Start gives a fresh limit', () =>
  withPlugin({}, async () => {
    const expired = allText(await mountComponent(C.InstanceCard, { instance: { ...STOPPED, stop_reason: 'expired', ttl_minutes: 240 } }))

    assert.ok(expired.includes('Stopped: time limit reached. Files are kept.'), expired)
    assert.ok(expired.includes('Starting it gives it a fresh time limit of 4 h.'), expired)

    const orphan = allText(await mountComponent(C.InstanceCard, { instance: { ...STOPPED, stop_reason: 'orphan' } }))

    assert.ok(orphan.includes('Found on x1carbon without a record. Files are kept.'), orphan)
    assert.ok(!orphan.includes('fresh time limit'), 'only an expired environment gets the fresh-limit note')

    const idle = allText(await mountComponent(C.InstanceCard, { instance: { ...STOPPED, stop_reason: 'idle' } }))

    assert.ok(idle.includes('Stopped after being idle. Files are kept.'), idle)
  })
)

await check('InstanceCard: running environments show the time left, the end time and a warning chip when close', () =>
  withPlugin({}, async () => {
    const far = allText(await mountComponent(C.InstanceCard, { instance: { ...PLAIN, expires_at: minutesFromNow(200) } }))

    assert.match(far, /Time left 3 h/)
    assert.ok(far.includes('ends'), far)
    assert.ok(!far.includes('Time limit in'), 'no warning chip far from the limit')

    const near = allText(await mountComponent(C.InstanceCard, { instance: { ...PLAIN, expires_at: minutesFromNow(10) } }))

    assert.match(near, /Time left 1\d min|Time left 10 min|Time left 9 min/)
    assert.match(near, /Time limit in \d+ min/, 'warning chip close to the limit')

    const warned = allText(await mountComponent(C.InstanceCard, { instance: { ...PLAIN, expires_at: minutesFromNow(60), expiry_warned_at: '2026-11-09T10:00:00Z' } }))

    assert.match(warned, /Time limit in/, 'a warned environment keeps the chip')

    const none = allText(await mountComponent(C.InstanceCard, { instance: { ...PLAIN, expires_at: null } }))

    assert.ok(none.includes('No time limit'), none)
  })
)

await check('instance.expiring: a warning toast with an Extend action that opens the Extend dialog', () =>
  withPlugin({}, async (ctx, fake) => {
    const payload = { id: 'i-ab12cd34', name: 'python-dev', host_id: 'x1carbon', expires_at: minutesFromNow(10), minutes_left: 10 }

    fake.emit('plugin.cloud-compute.instance.expiring', payload)

    const toast = callsOf('notify').map(c => c.args[0]).find(t => /cc-expiring/.test(t?.id ?? ''))

    assert.ok(toast, 'no toast for instance.expiring')
    assert.equal(toast.kind, 'warning')
    assert.ok(toast.message.includes('python-dev') && toast.message.includes('10 minutes'), toast.message)
    assert.ok(/files are kept/i.test(toast.message), toast.message)
    assert.equal(toast.action?.label, 'Extend')

    toast.action.onClick()
    assert.equal(callsOf('navigate').length, 1, 'Extend opens the page')

    const tree = await mountComponent(C.OverviewTab)

    assert.ok(openDialog(tree, 'ExtendDialog'), 'the Extend dialog should open for that environment')
    assert.equal(openDialog(tree, 'ExtendDialog').props.instance.id, 'i-ab12cd34')
  })
)

await check('ExtendDialog: presets and a custom field send POST /instances/{id}/extend {minutes}', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.ExtendDialog, { instance: PLAIN, onClose() {} })

    for (const label of ['30 min', '1 h', '4 h', 'Extend']) {
      assert.ok(buttonsOf(tree).includes(label), `missing "${label}" (have ${buttonsOf(tree)})`)
    }

    tree = await click(findButtons(tree).byLabel('4 h'))
    tree = await click(findButtons(tree).byLabel('Extend'))
    assert.deepEqual(bodyOf(callsTo(fake, 'POST', '/instances/i-ab12cd34/extend')[0]), { minutes: 240 })

    tree = await mountComponent(C.ExtendDialog, { instance: PLAIN, onClose() {} })
    tree = await setValue(inputField(tree, ['minutes to add (1-10080)'], 'ExtendDialog'), '90')
    tree = await click(findButtons(tree).byLabel('Extend'))
    assert.deepEqual(bodyOf(callsTo(fake, 'POST', '/instances/i-ab12cd34/extend')[1]), { minutes: 90 })
  })
)

await check('ExtendDialog: values outside 1-10080 are refused before any request', () =>
  withPlugin({}, async (ctx, fake) => {
    for (const bad of ['0', '10081', 'abc', '']) {
      let tree = await mountComponent(C.ExtendDialog, { instance: PLAIN, onClose() {} })

      tree = await setValue(inputField(tree, ['minutes to add (1-10080)'], 'ExtendDialog'), bad)
      tree = await click(findButtons(tree).byLabel('Extend'))
      assert.ok(allText(tree).includes('1 to 10080'), `no error for "${bad}"`)
    }

    assert.equal(callsTo(fake, 'POST', '/instances/i-ab12cd34/extend').length, 0)
  })
)

await check('InstanceCard: Start on an expired environment explains the fresh limit first; other stopped ones start at once', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.InstanceCard, { instance: { ...STOPPED, stop_reason: 'expired', ttl_minutes: 240 } })

    tree = await click(findButtons(tree).byLabel('Start'))
    assert.equal(callsTo(fake, 'POST', '/instances/i-0c9d8e7f/start').length, 0, 'start waits for the dialog')
    assert.ok(textOf(openConfirm(tree)).includes('fresh time limit of 4 h'), textOf(openConfirm(tree)))
    tree = await click(findButtons(openConfirm(tree)).byLabel('Start'))
    assert.equal(callsTo(fake, 'POST', '/instances/i-0c9d8e7f/start').length, 1)

    tree = await mountComponent(C.InstanceCard, { instance: { ...STOPPED, stop_reason: 'user' } })
    tree = await click(findButtons(tree).byLabel('Start'))
    assert.equal(callsTo(fake, 'POST', '/instances/i-0c9d8e7f/start').length, 2)
    assert.equal(openConfirm(tree), undefined, 'no dialog for a stopped-by-user environment')
  })
)

await check('DeleteDialog: saves first by default, then shows where the copy was stored', () =>
  withPlugin({ 'POST /instances/i-ab12cd34/destroy': { fixture: 'instance_destroyed.json' } }, async (ctx, fake) => {
    T.$deleted.set(null)

    let closed = 0
    let tree = await mountComponent(C.DeleteDialog, { instance: PLAIN, onClose: () => { closed += 1 } })
    const text = allText(tree)

    assert.ok(text.includes('Deletes python-dev and everything in it.'), text)
    assert.ok(text.includes('Save the files in /work to this computer first (recommended)'), text)
    assert.ok(text.includes('Files outside /work are not saved.'), 'says plainly what is not saved')
    assert.ok(!/its files were saved/i.test(text))
    assert.equal(findAll(tree, n => n.name === 'Checkbox')[0].props.checked, true, 'checked by default')

    tree = await click(findButtons(tree).byLabel('Delete'))
    assert.deepEqual(bodyOf(callsTo(fake, 'POST', '/instances/i-ab12cd34/destroy')[0]), { save: true })
    assert.ok(fake.restCalls.some(c => c.pathname === '/exports' && c.query.instance_id === 'i-ab12cd34'), 'looks up the saved copy')
    assert.equal(closed, 1)

    const shown = T.$deleted.get()

    assert.equal(shown.export.id, 'x-5e6f7a8b', 'the newest copy is shown')
    assert.ok(toastTexts().some(t => t.includes('were saved to') && t.includes('20261108T150100Z-before-delete')), 'the toast names the folder and the saved path')

    const notice = await mountComponent(C.DeletedNotice)

    assert.ok(allText(notice).includes(shown.export.local_path), 'the saved path is shown')
    assert.ok(allText(notice).includes(DESTROYED_FILES_SENTENCE) && allText(notice).includes('Files outside /work were not saved.'), allText(notice))
    assert.ok(findAll(notice, n => n.name === 'CopyButton').some(n => n.props.text === shown.export.local_path), 'with a Copy button')
  })
)

await check('DeleteDialog: the backend message about the save is shown as it is in the toast', () =>
  withPlugin({ 'POST /instances/i-ab12cd34/destroy': async () => ({ ...readFixture('instance_action.json'), message: 'Nothing in /work needed saving' }), 'GET /exports': { body: { exports: [], folder: EXPORTS.folder } } }, async () => {
    let tree = await mountComponent(C.DeleteDialog, { instance: PLAIN, onClose() {} })

    tree = await click(findButtons(tree).byLabel('Delete'))
    assert.ok(toastTexts().some(t => t.includes('Nothing in /work needed saving')), toastTexts().join('|'))
  })
)

await check('DeleteDialog: a failed save deletes nothing and offers a separate, confirmed Delete without saving', () => {
  const bodies = []

  return withPlugin(
    {
      'POST /instances/i-ab12cd34/destroy': async call => {
        bodies.push(bodyOf(call))
        if (bodyOf(call).save === false) return readFixture('instance_action.json')
        throw restError(409, { error: { code: 'not_saved', message: 'Files could not be saved, so nothing was deleted: disk full. Delete without saving to remove it anyway.' } })
      }
    },
    async (ctx, fake) => {
      let tree = await mountComponent(C.DeleteDialog, { instance: PLAIN, onClose() {} })

      tree = await click(findButtons(tree).byLabel('Delete'))
      assert.ok(allText(tree).includes('nothing was deleted: disk full'), allText(tree))
      assert.ok(buttonsOf(tree).includes('Delete without saving'), `the dangerous button is offered (have ${buttonsOf(tree)})`)
      assert.deepEqual(bodies, [{ save: true }])

      tree = await click(findButtons(tree).byLabel('Delete without saving'))
      assert.deepEqual(bodies, [{ save: true }], 'a second confirmation comes first')
      assert.ok(openConfirm(tree), 'a ConfirmDialog is shown')
      assert.ok(textOf(openConfirm(tree)).includes('cannot be recovered'), textOf(openConfirm(tree)))

      tree = await click(findButtons(openConfirm(tree)).byLabel('Delete without saving'))
      assert.deepEqual(bodies, [{ save: true }, { save: false }])
      assert.ok(!fake.restCalls.some(c => c.pathname === '/exports'), 'nothing was saved, so no saved copy is looked up')
    }
  )
})

await check('DeleteDialog: unticking the box turns Delete into Delete without saving, behind a second confirm', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.DeleteDialog, { instance: PLAIN, onClose() {} })

    tree = await setValue(findAll(tree, n => n.name === 'Checkbox')[0], false)
    assert.ok(!buttonsOf(tree).includes('Delete'), buttonsOf(tree))
    assert.ok(buttonsOf(tree).includes('Delete without saving'))
    assert.ok(allText(tree).includes('lost for good'), allText(tree))

    tree = await click(findButtons(tree).byLabel('Delete without saving'))
    assert.equal(callsTo(fake, 'POST', '/instances/i-ab12cd34/destroy').length, 0)
    assert.ok(openConfirm(tree))
    tree = await click(findButtons(openConfirm(tree)).byLabel('Delete without saving'))
    assert.deepEqual(bodyOf(callsTo(fake, 'POST', '/instances/i-ab12cd34/destroy')[0]), { save: false })
  })
)

await check('OverviewTab shows the saved-copy notice after a delete and closes it', () =>
  withPlugin({}, async () => {
    T.$deleted.set({ name: 'python-dev', instance_id: 'i-ab12cd34', export: FETCHED })

    let tree = await mountComponent(C.OverviewTab)

    assert.ok(allText(tree).includes('python-dev was deleted'), 'notice title')
    assert.ok(allText(tree).includes(FETCHED.local_path))
    tree = await click(findButtons(openDialog(tree, 'DeletedNotice')).byLabel('Close'))
    assert.equal(T.$deleted.get(), null)
  })
)

await check('FilesDialog: breadcrumbs, folders, Download and Save all of /work', () =>
  withPlugin(
    {
      'GET /instances/i-ab12cd34/files': async call =>
        call.query.path === '/work/results'
          ? { path: '/work/results', entries: [{ name: 'out.csv', type: 'file', size: 5242880, mtime: '2026-11-08T14:10:00Z' }], truncated: false }
          : readFixture('files_list.json')
    },
    async (ctx, fake) => {
      let tree = await mountComponent(C.FilesDialog, { instance: PLAIN, onClose() {} })
      let text = allText(tree)

      assert.ok(text.includes('Files: python-dev'), text)
      assert.ok(text.includes('results/') && text.includes('notes.txt'), text)
      assert.ok(text.includes('1.3 KB'), 'sizes are readable')
      for (const label of ['/', 'work', 'Save all of /work', 'Upload\u2026']) assert.ok(buttonsOf(tree).includes(label), `missing "${label}" (have ${buttonsOf(tree)})`)
      assert.deepEqual(callsTo(fake, 'GET', '/instances/i-ab12cd34/files')[0].query, {}, 'the first listing is the work folder')

      tree = await click(findButtons(tree).byLabel('results/'))
      assert.equal(callsTo(fake, 'GET', '/instances/i-ab12cd34/files').at(-1).query.path, '/work/results')
      assert.ok(allText(tree).includes('out.csv') && allText(tree).includes('5 MB'), allText(tree))
      assert.deepEqual(findAll(tree, n => n.type === 'nav')[0].children.map(c => textOf(c)), ['/', 'work', 'results'])

      tree = await click(findButtons(tree).byLabel('Download'))
      assert.equal(callsTo(fake, 'POST', '/instances/i-ab12cd34/files/fetch').length, 0, 'the size and a confirmation come first')
      assert.equal(callsTo(fake, 'GET', '/instances/i-ab12cd34/files/size')[0].query.path, '/work/results/out.csv')
      tree = await click(findButtons(tree).byLabel('Save 2 MB, 12 files'))
      assert.deepEqual(bodyOf(callsTo(fake, 'POST', '/instances/i-ab12cd34/files/fetch')[0]), { path: '/work/results/out.csv' })
      text = allText(tree)
      assert.ok(text.includes(FETCHED.local_path), 'the saved path is shown')
      assert.ok(findAll(tree, n => n.name === 'CopyButton').some(n => n.props.text === FETCHED.local_path), 'with a Copy button')

      tree = await click(findButtons(openDialog(tree, 'SaveDialog')).byLabel('Close'))
      tree = await click(findButtons(tree).byLabel('work'))
      tree = await click(findButtons(tree).byLabel('Save all of /work'))
      tree = await click(findButtons(tree).byLabel('Save 2 MB, 12 files'))
      assert.deepEqual(bodyOf(callsTo(fake, 'POST', '/instances/i-ab12cd34/files/fetch')[1]), { path: '/work' })
    }
  )
)

await check('FilesDialog: saved copies list size, date, reason, the folder with Copy; Restore and Delete work', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.FilesDialog, { instance: PLAIN, onClose() {} })
    const text = allText(tree)

    assert.ok(fake.restCalls.some(c => c.pathname === '/exports' && c.query.instance_id === 'i-ab12cd34'))
    assert.ok(text.includes('Saved copies'))
    assert.ok(text.includes(EXPORTS.folder), 'the exports folder is shown')
    assert.ok(findAll(tree, n => n.name === 'CopyButton').some(n => n.props.text === EXPORTS.folder), 'with Copy')
    assert.ok(text.includes('1.9 MB') && text.includes('saved before delete'), text)

    const rows = findAll(tree, n => n.name === 'ExportRow')

    assert.equal(rows.length, 2)
    assert.ok(buttonsOf(rows[0]).includes('Restore into\u2026') && buttonsOf(rows[0]).includes('Delete'))

    tree = await click(findButtons(rows[0]).byLabel('Restore into\u2026'))
    assert.ok(openDialog(tree, 'RestoreDialog'), 'the restore dialog opens')
    assert.ok(allText(openDialog(tree, 'RestoreDialog')).includes('Files with the same names there are replaced'))
    tree = await click(findButtons(openDialog(tree, 'RestoreDialog')).byLabel('Restore…'))
    assert.equal(callsTo(fake, 'POST', '/exports/x-5e6f7a8b/restore').length, 0, 'nothing is sent before the confirmation')
    tree = await click(findButtons(openConfirm(tree)).byLabel('Restore and replace'))
    assert.deepEqual(bodyOf(callsTo(fake, 'POST', '/exports/x-5e6f7a8b/restore')[0]), { instance_id: 'i-ab12cd34', dest: '/work' })
    assert.ok(allText(tree).includes('Restored into /work'), allText(tree))

    tree = await mountComponent(C.FilesDialog, { instance: PLAIN, onClose() {} })
    tree = await click(findButtons(findAll(tree, n => n.name === 'ExportRow')[0]).byLabel('Delete'))
    assert.equal(callsTo(fake, 'DELETE', '/exports/x-5e6f7a8b').length, 0, 'delete waits for the confirm')
    assert.ok(textOf(openConfirm(tree)).includes('cannot be recovered'))
    tree = await click(findButtons(openConfirm(tree)).byLabel('Delete'))
    assert.equal(callsTo(fake, 'DELETE', '/exports/x-5e6f7a8b').length, 1)
  })
)

await check('FilesDialog: a stopped environment says Start it to browse files, but Save all and saved copies still work', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.FilesDialog, { instance: STOPPED, onClose() {} })

    assert.ok(allText(tree).includes('Start it to browse files'), allText(tree))
    assert.equal(callsTo(fake, 'GET', '/instances/i-0c9d8e7f/files').length, 0, 'a stopped container is not listed')
    assert.ok(fake.restCalls.some(c => c.pathname === '/exports' && c.query.instance_id === 'i-0c9d8e7f'))
    assert.ok(allText(tree).includes('Saved copies'))
    tree = await click(findButtons(tree).byLabel('Save all of /work'))
    tree = await click(findButtons(tree).byLabel('Save 2 MB, 12 files'))
    assert.deepEqual(bodyOf(callsTo(fake, 'POST', '/instances/i-0c9d8e7f/files/fetch')[0]), { path: '/work' })
  })
)

await check('FilesDialog: Upload asks first, explaining that files leave this computer', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.FilesDialog, { instance: PLAIN, onClose() {} })

    tree = await click(findButtons(tree).byLabel('Upload\u2026'))
    tree = await setValue(inputField(tree, ['path on this computer'], 'FilesDialog'), 'C:/data/input.csv')
    tree = await click(findButtons(tree).allByLabel('Upload\u2026').at(-1))
    assert.equal(callsTo(fake, 'POST', '/instances/i-ab12cd34/files/put').length, 0, 'upload waits for the confirm')
    assert.ok(textOf(openConfirm(tree)).includes('sends C:/data/input.csv from this computer to /work in python-dev'), textOf(openConfirm(tree)))
    tree = await click(findButtons(openConfirm(tree)).byLabel('Upload'))
    assert.deepEqual(bodyOf(callsTo(fake, 'POST', '/instances/i-ab12cd34/files/put')[0]), { local_path: 'C:/data/input.csv', dest: '/work' })
  })
)

await check('file errors read clearly: too large, not running, refused local path', () =>
  withPlugin(
    {
      'POST /instances/i-ab12cd34/files/fetch': { status: 413, body: { error: { code: 'too_large', message: 'The files in /work are larger than the export limit (2048 MB). Raise "Export max mb" in Settings or export a smaller folder.' } } },
      'POST /instances/i-ab12cd34/files/put': { status: 403, body: { error: { code: 'invalid', message: 'C:/Users/x/.ssh is in a private folder (SSH keys or Hermes data) and is never uploaded.' } } }
    },
    async (ctx, fake) => {
      assert.match(T.fileErrorText({ code: 'not_running', message: 'Start python-dev to browse its files.' }), /^Not running\./)
      assert.match(T.fileErrorText({ code: 'too_large', message: 'x' }), /^Too large\. x/)
      assert.match(T.fileErrorText({ status: 403, message: 'private' }), /^Refused\. private/)

      let tree = await mountComponent(C.FilesDialog, { instance: PLAIN, onClose() {} })

      tree = await click(findButtons(tree).byLabel('Save all of /work'))
      tree = await click(findButtons(tree).byLabel('Save 2 MB, 12 files'))
      assert.ok(allText(tree).includes('Too large. The files in /work are larger than the export limit'), allText(tree))

      tree = await click(findButtons(tree).byLabel('Upload\u2026'))
      tree = await setValue(inputField(tree, ['path on this computer'], 'FilesDialog'), 'C:/Users/x/.ssh')
      tree = await click(findButtons(tree).allByLabel('Upload\u2026').at(-1))
      await assert.rejects(() => click(findButtons(openConfirm(tree)).byLabel('Upload')), /Refused\. C:\/Users\/x\/\.ssh is in a private folder/)
    }
  )
)

await check('FilesDialog: listing a container that stopped meanwhile says Not running', () =>
  withPlugin({ 'GET /instances/i-ab12cd34/files': { status: 409, body: { error: { code: 'not_running', message: 'Start python-dev to browse its files.' } } } }, async () => {
    const tree = await mountComponent(C.FilesDialog, { instance: PLAIN, onClose() {} })

    assert.ok(allText(tree).includes('Not running. Start the environment, then try again.'), allText(tree))
  })
)

await check('Help falls back to a topic list that includes Keeping your data', () =>
  withPlugin({ 'GET /docs': { status: 500 } }, async () => {
    const tree = await mountComponent(C.HelpTab)

    assert.ok(allText(tree).includes('Keeping your data'), 'fallback topic list')
  })
)

// =============================================================================
// Templates UI.
// =============================================================================
const TEMPLATES = readFixture('templates.json')
const HOSTS = readFixture('hosts.json')
const tplById = id => TEMPLATES.find(t => t.id === id)
// GET /templates/{id}?default=1: the shipped version of a built-in, 404 for anything else.
const shippedOverride = (changed = {}) => async call => {
  const id = decodeURIComponent(call.pathname.split('/').pop())
  const t = TEMPLATES.find(x => x.id === id && x.builtin)

  if (!t) throw restError(404, { error: { code: 'not_found', message: `template ${id} has no default version` } })

  return { ...t, ...(changed[id] || {}) }
}
const cardFor = (tree, id) => findAll(tree, n => n.name === 'TemplateCard' && n.props?.template?.id === id)[0]
const SHIPPED = { 'GET /templates/{id}': shippedOverride() }

await check('isAgentTemplate: an agent object that is enabled and in mode hermes or script', () => {
  assert.equal(T.isAgentTemplate(tplById('hermes-agent')), true)
  assert.equal(T.isAgentTemplate({ agent: { enabled: true, mode: 'script' } }), true)
  assert.equal(T.isAgentTemplate({ agent: { mode: 'hermes' } }), true)
  assert.equal(T.isAgentTemplate({ agent: { enabled: false, mode: 'hermes' } }), false)
  assert.equal(T.isAgentTemplate({ agent: { enabled: true, mode: 'other' } }), false)
  assert.equal(T.isAgentTemplate({ agent: null }), false)
  assert.equal(T.isAgentTemplate(tplById('python-dev')), false)
})

await check('sameTemplate ignores the builtin flag only', () => {
  const a = tplById('python-dev')

  assert.equal(T.sameTemplate(a, { ...a, builtin: false }), true)
  assert.equal(T.sameTemplate(a, { ...a, cpus: 8 }), false)
  assert.equal(T.sameTemplate(a, { ...a, variables: [{ name: 'X', source: 'literal:1', scope: 'runtime', hosts: null, description: '' }] }), false)
})

await check('TemplatesTab: environment and cloud agent templates are separate sections; agent ones have no Spawn', () =>
  withPlugin(SHIPPED, async () => {
    const tree = await mountComponent(C.TemplatesTab)
    const text = allText(tree)

    assert.ok(text.includes('Environment templates') && text.includes('Cloud agent templates'), text)
    assert.ok(text.includes('The Agents tab uses these'), text)
    assert.ok(buttonsOf(tree).includes('+ New template'))

    const agent = cardFor(tree, 'hermes-agent')
    const env = cardFor(tree, 'python-dev')

    assert.ok(!buttonsOf(agent).includes('Spawn on\u2026'), 'an agent template is not spawned from here')
    assert.ok(buttonsOf(env).includes('Spawn on\u2026'))
    assert.ok(textOf(agent).includes('Used by the Agents tab'), textOf(agent))

    const sections = findAll(tree, n => n.name === 'Section')
    const titleOf = n => n.props.title
    const envSection = sections.find(n => titleOf(n) === 'Environment templates')
    const agentSection = sections.find(n => titleOf(n) === 'Cloud agent templates')

    assert.ok(findAll(envSection, n => n.name === 'TemplateCard' && n.props.template.id === 'python-dev').length === 1)
    assert.ok(findAll(envSection, n => n.name === 'TemplateCard' && n.props.template.id === 'hermes-agent').length === 0)
    assert.ok(findAll(agentSection, n => n.name === 'TemplateCard' && n.props.template.id === 'hermes-agent').length === 1)
  })
)

await check('TemplatesTab: an agent object that is disabled stays in the environment section', () =>
  withPlugin(
    { ...SHIPPED, 'GET /templates': { body: [{ ...tplById('ml-gpu'), agent: { enabled: false, mode: 'hermes' } }, { ...tplById('hermes-agent'), id: 'agent-script', name: 'Scripted', agent: { enabled: true, mode: 'script' } }] } },
    async () => {
      const tree = await mountComponent(C.TemplatesTab)
      const agentSection = findAll(tree, n => n.name === 'Section' && n.props.title === 'Cloud agent templates')[0]

      assert.deepEqual(findAll(agentSection, n => n.name === 'TemplateCard').map(n => n.props.template.id), ['agent-script'])
      assert.ok(buttonsOf(cardFor(tree, 'ml-gpu')).includes('Spawn on\u2026'))
    }
  )
)

await check('TemplatesTab: built-ins show Built-in and have no Delete; user templates have Delete and no badge', () =>
  withPlugin(SHIPPED, async () => {
    const tree = await mountComponent(C.TemplatesTab)
    const builtin = cardFor(tree, 'python-dev')
    const user = cardFor(tree, 'ml-gpu')

    assert.ok(textOf(builtin).includes('Built-in') && !textOf(builtin).includes('Modified'), textOf(builtin))
    assert.ok(!buttonsOf(builtin).includes('Delete') && !buttonsOf(builtin).includes('Reset to default'), buttonsOf(builtin))
    assert.ok(buttonsOf(user).includes('Delete') && !textOf(user).includes('Built-in'), textOf(user))
  })
)

await check('TemplatesTab: an edited built-in (flag off, id shipped) shows Modified and Reset to default, then resets after a confirm', () =>
  withPlugin(
    { 'GET /templates/{id}': shippedOverride(), 'GET /templates': { body: TEMPLATES.map(t => (t.id === 'python-dev' ? { ...t, builtin: false, cpus: 8 } : t)) } },
    async (ctx, fake) => {
      let tree = await mountComponent(C.TemplatesTab)
      const card = cardFor(tree, 'python-dev')

      assert.ok(callsTo(fake, 'GET', '/templates/python-dev').some(c => c.query.default === '1'), 'compares with the shipped version')
      assert.ok(textOf(card).includes('Built-in') && textOf(card).includes('Modified'), textOf(card))
      assert.ok(buttonsOf(card).includes('Reset to default') && !buttonsOf(card).includes('Delete'), buttonsOf(card))

      tree = await click(findButtons(card).byLabel('Reset to default'))
      assert.equal(callsTo(fake, 'DELETE', '/templates/python-dev').length, 0, 'reset waits for the confirm')
      assert.ok(textOf(openConfirm(tree)).includes('the original comes back'), textOf(openConfirm(tree)))
      tree = await click(findButtons(openConfirm(tree)).byLabel('Reset to default'))
      assert.equal(callsTo(fake, 'DELETE', '/templates/python-dev').length, 1)
    }
  )
)

await check('TemplatesTab: a stored built-in identical to the shipped one is not Modified, whatever its flag', () =>
  withPlugin({ 'GET /templates/{id}': shippedOverride(), 'GET /templates': { body: TEMPLATES.map(t => (t.id === 'python-dev' ? { ...t, builtin: false } : t)) } }, async () => {
    const tree = await mountComponent(C.TemplatesTab)

    assert.ok(!textOf(cardFor(tree, 'python-dev')).includes('Modified'))
    assert.ok(textOf(cardFor(tree, 'python-dev')).includes('Built-in'))
  })
)

await check('NewTemplateDialog: name and image only -> POST /templates {name, image}', () =>
  withPlugin(SHIPPED, async (ctx, fake) => {
    let tree = await mountComponent(C.TemplatesTab)

    tree = await click(findButtons(tree).byLabel('+ New template'))
    assert.ok(openDialog(tree, 'NewTemplateDialog'))

    tree = await click(findButtons(openDialog(tree, 'NewTemplateDialog')).byLabel('Create'))
    assert.equal(callsTo(fake, 'POST', '/templates').length, 0, 'name and image are required')
    assert.ok(allText(tree).includes('Give the template a name'), allText(tree))

    tree = await setValue(inputField(tree, ['name'], 'NewTemplateDialog'), ' My lab ')
    tree = await setValue(inputField(tree, ['image'], 'NewTemplateDialog'), 'docker.io/library/python:3.12-slim')
    tree = await click(findButtons(openDialog(tree, 'NewTemplateDialog')).byLabel('Create'))
    assert.deepEqual(bodyOf(callsTo(fake, 'POST', '/templates')[0]), { name: 'My lab', image: 'docker.io/library/python:3.12-slim' })
  })
)

await check('NewTemplateDialog: Start from copies the settings of an existing template', () =>
  withPlugin(SHIPPED, async (ctx, fake) => {
    let tree = await mountComponent(C.NewTemplateDialog, { templates: TEMPLATES, onClose() {}, onCreated() {} })
    const base = tplById('ml-gpu')

    tree = await setValue(findAll(tree, n => n.name === 'Select')[0], 'ml-gpu')
    assert.equal(inputField(tree, ['image'], 'NewTemplateDialog').props.value, base.image, 'the image is prefilled from the template')
    tree = await setValue(inputField(tree, ['name'], 'NewTemplateDialog'), 'Mine')
    tree = await click(findButtons(tree).byLabel('Create'))

    const body = bodyOf(callsTo(fake, 'POST', '/templates')[0])

    assert.equal(body.name, 'Mine')
    assert.equal(body.image, base.image)
    assert.deepEqual(body.variables, base.variables)
    assert.equal(body.cpus, base.cpus)
    assert.ok(!('id' in body) && !('builtin' in body), 'the id is generated by the backend and the flag is not copied')
  })
)

await check('TemplateEditor: a Name field renames any template and sends it with the PATCH', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.TemplateEditor, { template: tplById('ml-gpu'), onClose() {} })

    assert.equal(inputField(tree, ['name'], 'TemplateEditor').props.value, tplById('ml-gpu').name)
    tree = await setValue(inputField(tree, ['name'], 'TemplateEditor'), 'Lab GPU')
    tree = await click(findButtons(tree).byLabel('Save'))
    assert.equal(bodyOf(callsTo(fake, 'PATCH', '/templates/ml-gpu')[0]).name, 'Lab GPU')

    tree = await mountComponent(C.TemplateEditor, { template: tplById('ml-gpu'), onClose() {} })
    tree = await setValue(inputField(tree, ['name'], 'TemplateEditor'), '  ')
    tree = await click(findButtons(tree).byLabel('Save'))
    assert.equal(callsTo(fake, 'PATCH', '/templates/ml-gpu').length, 1, 'an empty name is not sent')
  })
)

await check('TemplateEditor: a built-in says changes become your version; variables show description, secret badge and when they are sent', () =>
  withPlugin({}, async () => {
    const note = 'Changes are saved as your version of this built-in. Reset to default restores the original.'
    const builtin = allText(await mountComponent(C.TemplateEditor, { template: tplById('python-dev'), builtin: true, onClose() {} }))
    const userTree = await mountComponent(C.TemplateEditor, { template: tplById('ml-gpu'), builtin: false, onClose() {} })
    const user = allText(userTree)

    assert.ok(builtin.includes(note), builtin)
    assert.ok(!user.includes(note), user)
    assert.ok(inputField(userTree, ['description'], 'TemplateEditor').props.value.includes('Hugging Face access token'), 'the variable description is shown')
    assert.ok(user.includes('secret'), 'secret badge')
    assert.ok(user.includes('Sent into the environment when it starts'), user)
  })
)

await check('TemplateEditor: Add variable includes a description, which is saved', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.TemplateEditor, { template: tplById('python-dev'), onClose() {} })

    tree = await click(findButtons(tree).byLabel('Add variable'))
    tree = await setValue(inputField(tree, ['variable name'], 'TemplateEditor'), 'MY_FLAG')
    tree = await setValue(inputField(tree, ['source'], 'TemplateEditor'), 'literal:1')
    tree = await setValue(inputField(tree, ['description'], 'TemplateEditor'), ' Turns the flag on ')
    tree = await click(findButtons(tree).byLabel('Save'))
    assert.deepEqual(bodyOf(callsTo(fake, 'PATCH', '/templates/python-dev')[0]).variables, [
      { name: 'MY_FLAG', source: 'literal:1', scope: 'redacted', hosts: null, description: 'Turns the flag on' }
    ])
  })
)

const spawnProps = (template, extra = {}) => ({ templates: TEMPLATES, hosts: HOSTS, template, hostId: 'x1carbon', onClose() {}, onSpawned() {}, ...extra })
const previewWith = (hfSent, extra = {}) => ({
  'GET /templates/ml-gpu/preview': async () => {
    const p = readFixture('template_preview.json')

    p.variables = p.variables.map(v => (v.name === 'HF_TOKEN' ? { ...v, will_send: hfSent, reason: hfSent ? '' : 'secret HF_TOKEN not set' } : v))

    return { ...p, ...extra }
  }
})

await check('SpawnDialog: lists every variable that will be sent, with its description, and what will not be', () =>
  withPlugin(previewWith(false), async () => {
    const tree = await mountComponent(C.SpawnDialog, spawnProps(tplById('ml-gpu')))
    const text = allText(tree)

    assert.ok(text.includes('HF_TOKEN') && text.includes('Hugging Face access token'), text)
    assert.ok(text.includes('not set, will not be sent'), text)
    assert.ok(text.includes('LANG') && text.includes('Language and character encoding'), text)
    assert.equal(findAll(tree, n => n.name === 'Checkbox').length, 0, 'no secret is sent, so nothing to confirm')
    assert.equal(findButtons(tree).byLabel('Spawn').props.disabled, false)
  })
)

await check('SpawnDialog: when a secret will be sent, Spawn stays off until the user ticks Send these secrets', () =>
  withPlugin(previewWith(true), async (ctx, fake) => {
    let tree = await mountComponent(C.SpawnDialog, spawnProps(tplById('ml-gpu')))
    const text = allText(tree)

    assert.ok(text.includes('Send these secrets into the environment'), text)
    assert.ok(text.includes('secret'), 'secret badge')
    assert.equal(findButtons(tree).byLabel('Spawn').props.disabled, true, 'disabled until ticked')
    assert.equal(findAll(tree, n => n.name === 'Checkbox')[0].props.checked, false)
    assert.equal(callsTo(fake, 'POST', '/instances').length, 0)

    tree = await setValue(findAll(tree, n => n.name === 'Checkbox')[0], true)
    assert.equal(findButtons(tree).byLabel('Spawn').props.disabled, false)
    tree = await click(findButtons(tree).byLabel('Spawn'))
    assert.deepEqual(bodyOf(callsTo(fake, 'POST', '/instances')[0]), { template_id: 'ml-gpu', host_id: 'x1carbon' })
  })
)

await check('SpawnDialog: unticking again turns Spawn off', () =>
  withPlugin(previewWith(true), async () => {
    let tree = await mountComponent(C.SpawnDialog, spawnProps(tplById('ml-gpu')))

    tree = await setValue(findAll(tree, n => n.name === 'Checkbox')[0], true)
    tree = await setValue(findAll(tree, n => n.name === 'Checkbox')[0], false)
    assert.equal(findButtons(tree).byLabel('Spawn').props.disabled, true)
  })
)

await check('SpawnDialog: without a preview nothing can be spawned', () =>
  withPlugin({ 'GET /templates/ml-gpu/preview': { status: 500 } }, async (ctx, fake) => {
    const tree = await mountComponent(C.SpawnDialog, spawnProps(tplById('ml-gpu')))

    assert.ok(allText(tree).includes("Couldn't check what will be sent"), allText(tree))
    assert.equal(findButtons(tree).byLabel('Spawn').props.disabled, true)
    assert.equal(callsTo(fake, 'POST', '/instances').length, 0)
  })
)

await check('SpawnDialog: a description sent by the preview wins; agent templates are not offered in the picker', () =>
  withPlugin(
    {
      'GET /templates/ml-gpu/preview': async () => {
        const p = readFixture('template_preview.json')

        return { ...p, variables: p.variables.map(v => (v.name === 'LANG' ? { ...v, description: 'From the preview' } : v)) }
      }
    },
    async () => {
      const tree = await mountComponent(C.SpawnDialog, spawnProps(tplById('ml-gpu')))

      assert.ok(allText(tree).includes('From the preview'))

      const open = await mountComponent(C.SpawnDialog, spawnProps(null, { hostId: '' }))
      const select = findAll(open, n => n.name === 'Select')[0]
      const labels = findAll(select, n => n.name === 'SelectItem').map(n => textOf(n))

      assert.ok(labels.length > 0 && !labels.includes(tplById('hermes-agent').name), labels.join(', '))
    }
  )
)

await check('secretsToSend: only secrets that will be sent', () => {
  const preview = readFixture('template_preview.json')

  assert.deepEqual(T.secretsToSend(preview), [], 'HF_TOKEN is not set in the fixture')
  assert.deepEqual(T.secretsToSend({ variables: [{ name: 'A', source_kind: 'secret', will_send: true }, { name: 'B', source_kind: 'literal', will_send: true }] }).map(v => v.name), ['A'])
})

// =============================================================================
// Path fields, the delete dialog and the stopped environment card.
// =============================================================================
const SCRIPT_TEMPLATE = { id: 'agent-script', name: 'Scripted cloud agent', kind: 'container', agent: { enabled: true, mode: 'script' } }
const withScriptTemplate = () => [...readFixture('templates.json'), SCRIPT_TEMPLATE]
const draftOf = fake => fake.storageData.get('agent-draft')
const fieldValue = (tree, keys, where) => inputField(tree, keys, where).props.value
const runById = id => AGENTS.find(a => a.id === id)

await check('running and starting environments come first, stopped ones sit under a collapsible Stopped (N) group', () => {
  const stoppedNew = { ...STOPPED, id: 'i-0000aaaa', name: 'recent-stop', last_activity_at: '2026-11-08T13:00:00Z' }
  const creating = { ...PLAIN, id: 'i-0000bbbb', name: 'booting', status: 'creating', last_activity_at: '2026-11-08T14:30:00Z' }
  const list = [STOPPED, stoppedNew, creating, PLAIN, DISPLAY]

  assert.deepEqual(T.splitInstances(list).active.map(i => i.name), ['python-dev', 'desktop-novnc', 'booting'], 'running before starting')
  assert.deepEqual(T.splitInstances(list).stopped.map(i => i.name), ['recent-stop', STOPPED.name], 'stopped: most recently active first')

  return withPlugin({ 'GET /instances': { body: list } }, async ctx => {
    let tree = await mountComponent(C.OverviewTab)
    const order = () => findAll(tree, n => n.name === 'InstanceCard').map(n => n.props.instance.name)
    const heading = () => findAll(tree, n => n.type === 'button' && textOf(n).includes('Stopped (2)'))[0]

    assert.deepEqual(order(), ['python-dev', 'desktop-novnc', 'booting', 'recent-stop', 'data-prep'])
    assert.ok(heading(), 'a Stopped (2) heading')

    tree = await click(heading())
    assert.deepEqual(order(), ['python-dev', 'desktop-novnc', 'booting'], 'collapsing hides the stopped cards')
    assert.equal(ctx.storage.get('stopped-folded', false), true, 'the fold is remembered')
    tree = await click(heading())
    assert.ok(order().includes('data-prep'))
  })
})

await check('with nothing running the stopped group still shows', () =>
  withPlugin({ 'GET /instances': { body: [STOPPED] } }, async () => {
    const tree = await mountComponent(C.OverviewTab)

    assert.ok(allText(tree).includes('Stopped (1)'), allText(tree))
    assert.equal(findAll(tree, n => n.name === 'InstanceCard').length, 1)
  })
)

await check('a file row separates the size from the date', () => {
  assert.equal(T.fileFacts({ type: 'file', size: 3355443, mtime: '2026-10-12T00:00:00Z' }).replace(/ \u00b7 .*/, ''), '3.2 MB')
  assert.ok(T.fileFacts({ type: 'file', size: 3355443, mtime: '2026-10-12T00:00:00Z' }).includes(' \u00b7 '))
  assert.equal(T.fileFacts({ type: 'dir', mtime: null }), 'folder')

  return withPlugin({}, async () => {
    const tree = await mountComponent(C.FilesDialog, { instance: PLAIN, onClose() {} })
    const text = allText(tree)

    assert.ok(!/(?:B|KB|MB|GB)(?=\d)/.test(text), 'size and date run together: ' + text)
    assert.ok(/1\.3 KB \u00b7 /.test(text), text)
  })
})

await check('Restore asks first, naming the saved copy, the environment and the folder; nothing is sent before Restore and replace', () =>
  withPlugin({}, async (ctx, fake) => {
    const item = EXPORTS.exports[0]
    let tree = await mountComponent(C.RestoreDialog, { item, current: PLAIN, onClose() {} })

    tree = await setValue(inputField(tree, ['destination folder'], 'RestoreDialog'), '/srv/app')
    tree = await click(findButtons(tree).byLabel('Restore\u2026'))
    assert.equal(callsTo(fake, 'POST', `/exports/${item.id}/restore`).length, 0, 'nothing is sent by the first button')

    const ask = textOf(openConfirm(tree))

    assert.ok(ask.includes('python-dev'), 'names the environment: ' + ask)
    assert.ok(ask.includes('/srv/app'), 'names the folder: ' + ask)
    assert.ok(ask.includes('1.9 MB') && ask.includes('/work'), 'names the saved copy: ' + ask)
    assert.ok(ask.includes('Files in /srv/app with the same names are replaced and cannot be recovered.'), ask)
    assert.equal(openConfirm(tree).props.destructive, true)
    assert.equal(openConfirm(tree).props.confirmLabel, 'Restore and replace')

    // Cancel sends nothing either.
    tree = await click(findButtons(openConfirm(tree)).byLabel('Cancel'))
    assert.ok(!openConfirm(tree))
    assert.equal(callsTo(fake, 'POST', `/exports/${item.id}/restore`).length, 0)

    tree = await click(findButtons(tree).byLabel('Restore\u2026'))
    tree = await click(findButtons(openConfirm(tree)).byLabel('Restore and replace'))
    assert.deepEqual(bodyOf(callsTo(fake, 'POST', `/exports/${item.id}/restore`)[0]), { instance_id: 'i-ab12cd34', dest: '/srv/app' })
    assert.ok(allText(tree).includes('Restored into'), allText(tree))
  })
)

await check('a failed restore shows the error and the first dialog stays usable', () =>
  withPlugin({ 'POST /exports/{id}/restore': { status: 409, body: { error: { code: 'not_running', message: 'Start python-dev first.' } } } }, async () => {
    let tree = await mountComponent(C.RestoreDialog, { item: EXPORTS.exports[0], current: PLAIN, onClose() {} })

    tree = await click(findButtons(tree).byLabel('Restore\u2026'))
    await assert.rejects(() => click(findButtons(openConfirm(tree)).byLabel('Restore and replace')), /Not running\./)
  })
)

await check('path fields drop surrounding spaces and one pair of matching quotes', () => {
  assert.equal(T.cleanPath('  "C:\\Users\\me\\My Docs\\a.csv"  '), 'C:\\Users\\me\\My Docs\\a.csv')
  assert.equal(T.cleanPath("'/home/me/data'"), '/home/me/data')
  assert.equal(T.cleanPath('\u201c/home/me/data\u201d'), '/home/me/data')
  assert.equal(T.cleanPath('""/odd""'), '"/odd"', 'only one pair is removed')
  assert.equal(T.cleanPath('"/mixed\''), '"/mixed\'', 'unmatched quotes stay')
  assert.equal(T.cleanPath('/it\'s/fine'), '/it\'s/fine')
  assert.equal(T.cleanPath('  '), '')
  assert.equal(T.cleanPath(null), '')

  return withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.FilesDialog, { instance: PLAIN, onClose() {} })

    tree = await click(findButtons(tree).byLabel('Upload\u2026'))
    tree = await setValue(inputField(tree, ['path on this computer'], 'FilesDialog'), '  "C:/Users/me/My Docs/input.csv" ')
    tree = await setValue(inputField(tree, ['destination folder in the environment'], 'FilesDialog'), " '/work/in' ")
    tree = await click(findButtons(tree).allByLabel('Upload\u2026').at(-1))
    assert.ok(textOf(openConfirm(tree)).includes('sends C:/Users/me/My Docs/input.csv from this computer to /work/in in python-dev'), textOf(openConfirm(tree)))
    tree = await click(findButtons(openConfirm(tree)).byLabel('Upload'))
    assert.deepEqual(bodyOf(callsTo(fake, 'POST', '/instances/i-ab12cd34/files/put')[0]), { local_path: 'C:/Users/me/My Docs/input.csv', dest: '/work/in' })
  })
})

await check('the restore destination and the repository path are cleaned too', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.RestoreDialog, { item: EXPORTS.exports[0], current: PLAIN, onClose() {} })

    tree = await setValue(inputField(tree, ['destination folder'], 'RestoreDialog'), ' "/srv/my app" ')
    tree = await click(findButtons(tree).byLabel('Restore\u2026'))
    tree = await click(findButtons(openConfirm(tree)).byLabel('Restore and replace'))
    assert.equal(bodyOf(callsTo(fake, 'POST', `/exports/${EXPORTS.exports[0].id}/restore`)[0]).dest, '/srv/my app')
    assert.equal(T.buildAgentRequest({ goal: 'g', host_id: 'h', repo_path: ' "C:/code/my repo" ' }).body.repo_path, 'C:/code/my repo')
  })
)

await check('each partition shows N running \u00b7 M pending from the jobs list, and nothing when both are zero', () => {
  const part = (name, def) => ({ name, default: def })

  assert.deepEqual(T.partitionLoad(readFixture('jobs.json'), 'slurm-x1', part('debug', true), 'debug'), { running: 1, pending: 0 })
  assert.deepEqual(T.partitionLoad(readFixture('jobs.json'), 'slurm-x1', part('batch', false), 'debug'), { running: 0, pending: 1 })
  assert.deepEqual(T.partitionLoad(readFixture('jobs.json'), 'slurm-x1', part('gpu', false), 'debug'), { running: 0, pending: 0 })
  assert.deepEqual(T.partitionLoad(readFixture('jobs.json'), 'other-host', part('debug', true), 'debug'), { running: 0, pending: 0 })
  assert.deepEqual(T.partitionLoad([{ host_id: 'slurm-x1', partition: null, state: 'RUNNING' }], 'slurm-x1', part('debug', true), 'debug'), { running: 1, pending: 0 }, 'no partition means the default one')

  return withPlugin({}, async () => {
    const tree = await mountComponent(C.JobsTab)
    const chips = findAll(tree, n => n.name === 'Badge').map(n => textOf(n)).filter(t => /running/.test(t))

    assert.deepEqual(chips.sort(), ['0 running \u00b7 1 pending', '1 running \u00b7 0 pending'], 'one chip for debug, one for batch, none for gpu')
  })
})

await check('the dialog says Task for the agent, with help, a concrete placeholder and an optional Done when line sent only when filled', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.AgentsTab)

    tree = await click(findButtons(tree).byLabel('New agent run\u2026'))

    const dialog = () => openDialog(tree, 'NewAgentDialog')
    const text = allText(dialog())

    assert.ok(text.includes('Task for the agent'), text)
    assert.ok(!/\bGoal\b/.test(text), 'the word Goal is gone: ' + text)
    assert.ok(text.includes('Describe what the agent should do in the repository. It works on its own until the task is done or the turn limit is reached.'), text)
    assert.ok(text.includes('Done when (optional)') && text.includes('How the agent can tell the task is complete.'), text)
    assert.match(inputField(dialog(), ['task for the agent'], 'NewAgentDialog').props.placeholder, /^For example: .{20,}/)

    tree = await setValue(inputField(dialog(), ['task for the agent'], 'NewAgentDialog'), 'Add a --verbose flag')
    tree = await setValue(inputField(dialog(), ['repository path on this computer'], 'NewAgentDialog'), '/home/dev/code/demo')
    tree = await click(findButtons(dialog()).byLabel('Start'))
    assert.ok(!('criteria' in bodyOf(callsMatching(fake, 'POST', /^\/agents$/)[0])), 'criteria is not sent when empty')

    tree = await click(findButtons(tree).byLabel('New agent run\u2026'))
    tree = await setValue(inputField(dialog(), ['task for the agent'], 'NewAgentDialog'), 'Add a --verbose flag')
    tree = await setValue(inputField(dialog(), ['repository path on this computer'], 'NewAgentDialog'), '/home/dev/code/demo')
    tree = await setValue(inputField(dialog(), ['done when (optional)'], 'NewAgentDialog'), '  all tests pass  ')
    tree = await click(findButtons(dialog()).byLabel('Start'))
    assert.equal(bodyOf(callsMatching(fake, 'POST', /^\/agents$/)[1]).criteria, 'all tests pass')
  })
)

await check('a draft survives closing the dialog, switching tabs and leaving the page; Draft restored + Discard draft; a start clears it', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.AgentsTab)

    tree = await click(findButtons(tree).byLabel('New agent run\u2026'))
    assert.ok(!allText(openDialog(tree, 'NewAgentDialog')).includes('Draft restored'), 'a fresh dialog has no note')
    tree = await setValue(inputField(openDialog(tree, 'NewAgentDialog'), ['task for the agent'], 'NewAgentDialog'), 'Describe this repository in detail')
    tree = await setValue(inputField(openDialog(tree, 'NewAgentDialog'), ['repository path on this computer'], 'NewAgentDialog'), '/home/dev/code/demo')
    tree = await setValue(inputField(openDialog(tree, 'NewAgentDialog'), ['done when (optional)'], 'NewAgentDialog'), 'a README exists')
    assert.equal(draftOf(fake).goal, 'Describe this repository in detail', 'the draft is saved as it is typed')

    // Cancel, then a different tab (the whole tab unmounts), then back.
    tree = await click(findButtons(openDialog(tree, 'NewAgentDialog')).byLabel('Cancel'))
    assert.ok(!openDialog(tree, 'NewAgentDialog'))
    tree = await mountComponent(C.HelpTab)
    tree = await mountComponent(C.AgentsTab)
    tree = await click(findButtons(tree).byLabel('New agent run\u2026'))

    let dialog = openDialog(tree, 'NewAgentDialog')

    assert.ok(allText(dialog).includes('Draft restored'), allText(dialog))
    assert.equal(fieldValue(dialog, ['task for the agent'], 'NewAgentDialog'), 'Describe this repository in detail')
    assert.equal(fieldValue(dialog, ['repository path on this computer'], 'NewAgentDialog'), '/home/dev/code/demo')
    assert.equal(fieldValue(dialog, ['done when (optional)'], 'NewAgentDialog'), 'a README exists')

    // The same after a restart: a new registration reads ctx.storage.
    const saved = JSON.parse(JSON.stringify(draftOf(fake)))

    ctx.fake.dispose()
    ctx.fake.disposers.length = 0
    ctx.storage.set('agent-draft', saved)
    plugin.register(ctx)
    tree = await mountComponent(C.AgentsTab)
    tree = await click(findButtons(tree).byLabel('New agent run\u2026'))
    assert.equal(fieldValue(openDialog(tree, 'NewAgentDialog'), ['task for the agent'], 'NewAgentDialog'), 'Describe this repository in detail', 'restored from ctx.storage')

    // Leaving for Settings does not touch the draft.
    tree = await setValue(inputField(openDialog(tree, 'NewAgentDialog'), ['task for the agent'], 'NewAgentDialog'), 'Describe this repository in detail, then list risks')
    hostFake.hostCalls.length = 0
    assert.equal(draftOf(fake).goal.endsWith('list risks'), true)

    // Discard draft empties the fields and the stored copy.
    tree = await click(findButtons(openDialog(tree, 'NewAgentDialog')).byLabel('Discard draft'))
    dialog = openDialog(tree, 'NewAgentDialog')
    assert.equal(fieldValue(dialog, ['task for the agent'], 'NewAgentDialog'), '')
    assert.equal(draftOf(fake), undefined)
    assert.ok(!allText(dialog).includes('Draft restored'))
  })
)

await check('a successful start clears the draft; a failed one keeps it', () => {
  let failing = true

  return withPlugin({ 'POST /agents': async () => { if (failing) throw restError(400, { error: { code: 'invalid', message: 'Set the cloud agent model endpoint and model in Settings \u2192 Own Compute.' } }); return readFixture('agent_created.json') } }, async (ctx, fake) => {
    let tree = await mountComponent(C.AgentsTab)

    tree = await click(findButtons(tree).byLabel('New agent run\u2026'))
    tree = await setValue(inputField(openDialog(tree, 'NewAgentDialog'), ['task for the agent'], 'NewAgentDialog'), 'Long task text')
    tree = await setValue(inputField(openDialog(tree, 'NewAgentDialog'), ['repository path on this computer'], 'NewAgentDialog'), '/repo')
    tree = await click(findButtons(openDialog(tree, 'NewAgentDialog')).byLabel('Start'))
    assert.ok(openDialog(tree, 'NewAgentDialog'), 'the dialog stays open on an error')
    assert.equal(draftOf(fake).goal, 'Long task text', 'a failed start keeps the draft')

    // Following the Open settings link leaves the page; the text is still there.
    hostFake.hostCalls.length = 0
    await click(findButtons(tree).byLabel('Open settings'))
    assert.deepEqual(callsOf('navigate').map(c => c.args[0]), ['#/settings?plugin=cloud-compute'])
    tree = await click(findButtons(openDialog(tree, 'NewAgentDialog')).byLabel('Cancel'))
    tree = await mountComponent(C.AgentsTab)
    tree = await click(findButtons(tree).byLabel('New agent run\u2026'))
    assert.equal(fieldValue(openDialog(tree, 'NewAgentDialog'), ['task for the agent'], 'NewAgentDialog'), 'Long task text')

    failing = false
    tree = await click(findButtons(openDialog(tree, 'NewAgentDialog')).byLabel('Start'))
    assert.ok(!openDialog(tree, 'NewAgentDialog'))
    assert.equal(draftOf(fake), undefined, 'a successful start clears the draft')
    tree = await click(findButtons(tree).byLabel('New agent run\u2026'))
    assert.equal(fieldValue(openDialog(tree, 'NewAgentDialog'), ['task for the agent'], 'NewAgentDialog'), '')
  })
})

await check('running is green, failed red, stopped grey, done and collected neutral', () =>
  withPlugin({}, async () => {
    const look = status => {
      const run = AGENTS.find(a => a.status === status)

      return mountComponent(C.AgentRunCard, { run }).then(tree => ({
        tone: findAll(tree, n => n.name === 'StatusDot')[0].props.tone,
        badge: findAll(tree, n => n.name === 'Badge' && textOf(n) === status)[0]?.props.variant
      }))
    }

    assert.deepEqual(await look('running'), { tone: 'good', badge: 'success' })
    assert.deepEqual(await look('failed'), { tone: 'bad', badge: 'destructive' })
    assert.deepEqual(await look('stopped'), { tone: 'muted', badge: 'muted' })
    assert.deepEqual(await look('done'), { tone: 'muted', badge: 'muted' })
    assert.deepEqual(await look('collected'), { tone: 'muted', badge: 'muted' })
    assert.equal(T.agentTone('starting'), 'warn')
  })
)

await check('Get results explains itself; Results fetched replaces the button; new commits offer Get new results', () =>
  withPlugin({}, async (ctx, fake) => {
    const HEAD = 'b2c3d4e5f60718293a4b5c6d7e8f9012345678a1'
    const fetched = { ...runById('a-5e6f7a8b'), collected_ref: 'refs/heads/cloud/a-5e6f7a8b/x', worktree_path: '/home/dev/code/demo-a-5e6f7a8b', head_sha: HEAD }

    // never fetched
    let tree = await mountComponent(C.AgentRunCard, { run: runById('a-5e6f7a8b') })

    assert.ok(findButtons(tree).byLabel('Get results'))
    assert.equal(findButtons(tree).byLabel('Collect'), undefined, 'the old label is gone')
    assert.ok(allText(tree).includes("Brings the agent's branch to this computer as a new branch and worktree in your repository. Nothing is merged."), allText(tree))

    // fetched, nothing new: collected status
    tree = await mountComponent(C.AgentRunCard, { run: { ...fetched, status: 'collected', instance_id: 'i-ab12cd34' } })
    assert.ok(allText(tree).includes('Results fetched'), allText(tree))
    assert.ok(allText(tree).includes(fetched.worktree_path))
    assert.ok(findAll(tree, n => n.name === 'CopyButton').some(n => n.props.text === fetched.worktree_path), 'with Copy')
    assert.equal(findButtons(tree).byLabel('Get results'), undefined)
    assert.equal(findButtons(tree).byLabel('Get new results'), undefined)

    // fetched earlier, the agent is done again with the same head: still nothing new
    ctx.fake.dispose()
    ctx.fake.disposers.length = 0
    ctx.storage.set('agent-fetched', { [fetched.id]: HEAD })
    plugin.register(ctx)
    tree = await mountComponent(C.AgentRunCard, { run: { ...fetched, status: 'done', instance_id: 'i-ab12cd34' } })
    assert.equal(findButtons(tree).byLabel('Get new results'), undefined, 'same head: nothing new')
    assert.ok(allText(tree).includes('Results fetched'))

    // the agent committed again after the fetch
    tree = await mountComponent(C.AgentRunCard, { run: { ...fetched, status: 'done', instance_id: 'i-ab12cd34', head_sha: 'c3d4e5f60718293a4b5c6d7e8f9012345678a1b2' } })
    assert.ok(findButtons(tree).byLabel('Get new results'), 'new commits offer Get new results')
    assert.ok(allText(tree).includes('new commits since'), allText(tree))
    tree = await click(findButtons(tree).byLabel('Get new results'))
    assert.equal(callsMatching(fake, 'POST', /\/collect$/).length, 1)
    assert.equal(findButtons(tree).byLabel('Get new results'), undefined, 'right after a fetch the button is gone')
  })
)

await check('the Agents tab notes the fetched head when a run is collected', () =>
  withPlugin({}, async ctx => {
    const run = { ...runById('a-3b4c5d6e'), instance_id: 'i-ab12cd34' }

    await mountComponent(C.AgentRunCard, { run })
    assert.equal(ctx.storage.get('agent-fetched', {})[run.id], run.head_sha)
  })
)

await check('Run again opens New agent run filled in from the run, for any run that is not running', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.AgentsTab)
    const failed = runById('a-9c0d1e2f')

    assert.equal(findButtons(cardOf(tree, runById('a-1a2b3c4d').id)).byLabel('Run again\u2026'), undefined, 'a running run offers no Run again')
    for (const status of ['done', 'failed', 'stopped', 'collected']) {
      assert.ok(findButtons(cardOf(tree, agentById(status).id)).byLabel('Run again\u2026'), `${status} runs offer Run again`)
    }

    tree = await click(findButtons(cardOf(tree, failed.id)).byLabel('Run again\u2026'))
    tree = await click(findButtons(openDialog(tree, 'RerunDialog')).byLabel('Change everything\u2026'))

    const dialog = openDialog(tree, 'NewAgentDialog')

    assert.ok(dialog, 'Run again opens the dialog')
    assert.equal(fieldValue(dialog, ['task for the agent'], 'NewAgentDialog'), failed.goal)
    assert.equal(fieldValue(dialog, ['repository path on this computer'], 'NewAgentDialog'), failed.repo_path)
    assert.equal(fieldValue(dialog, ['branch name (optional)'], 'NewAgentDialog'), failed.branch)
    assert.equal(findAll(dialog, n => n.name === 'Pick').map(n => n.props.value).includes(failed.host_id), true, 'the host')
    assert.equal(findAll(dialog, n => n.name === 'Pick').map(n => n.props.value).includes(failed.template_id), true, 'the template')
    assert.equal(fieldValue(dialog, ['max turns'], 'NewAgentDialog'), '30')
    assert.ok(allText(dialog).includes(`Filled in from ${failed.id}`), allText(dialog))

    tree = await click(findButtons(dialog).byLabel('Start'))
    const body = bodyOf(callsMatching(fake, 'POST', /^\/agents$/)[0])

    assert.equal(body.goal, failed.goal)
    assert.equal(body.repo_path, failed.repo_path)
    assert.equal(body.branch, failed.branch)
    assert.equal(body.template, failed.template_id)
  })
)

await check('Run again asks before it replaces a draft that holds text', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.AgentsTab)

    tree = await click(findButtons(tree).byLabel('New agent run\u2026'))
    tree = await setValue(inputField(openDialog(tree, 'NewAgentDialog'), ['task for the agent'], 'NewAgentDialog'), 'My careful draft')
    tree = await click(findButtons(openDialog(tree, 'NewAgentDialog')).byLabel('Cancel'))
    tree = await click(findButtons(cardOf(tree, runById('a-9c0d1e2f').id)).byLabel('Run again\u2026'))
    tree = await click(findButtons(openDialog(tree, 'RerunDialog')).byLabel('Change everything\u2026'))
    assert.ok(openConfirm(tree), 'asks first')
    assert.ok(!openDialog(tree, 'NewAgentDialog'))
    assert.equal(draftOf(fake).goal, 'My careful draft', 'the draft is untouched until the user agrees')
    tree = await click(findButtons(openConfirm(tree)).byLabel('Cancel'))
    assert.equal(draftOf(fake).goal, 'My careful draft')
    tree = await click(findButtons(cardOf(tree, runById('a-9c0d1e2f').id)).byLabel('Run again\u2026'))
    tree = await click(findButtons(openDialog(tree, 'RerunDialog')).byLabel('Change everything\u2026'))
    tree = await click(findButtons(openConfirm(tree)).byLabel('Replace draft'))
    assert.equal(fieldValue(openDialog(tree, 'NewAgentDialog'), ['task for the agent'], 'NewAgentDialog'), runById('a-9c0d1e2f').goal)
  })
)

await check('the Hermes agent template is the default and the script template reads as a handoff test', () =>
  withPlugin({ 'GET /templates': { body: withScriptTemplate() } }, async () => {
    let tree = await mountComponent(C.AgentsTab)

    tree = await click(findButtons(tree).byLabel('New agent run\u2026'))

    const dialog = () => openDialog(tree, 'NewAgentDialog')
    const pick = () => findAll(dialog(), n => n.name === 'Pick').find(n => (n.props.options || []).some(o => o.value === 'agent-script'))

    assert.equal(pick().props.value, 'hermes-agent', 'Hermes agent is selected by default')
    assert.equal(pick().props.options.find(o => o.value === 'agent-script').label, 'Handoff test (no model)')
    assert.ok(!allText(dialog()).includes('Commits a marker file'), 'the line shows only for the handoff test')

    tree = await setValue(pick(), 'agent-script')
    assert.ok(allText(dialog()).includes('Commits a marker file to prove the branch round trip. Does not use a model.'), allText(dialog()))
  })
)

await check('a finished script-mode run card says Handoff test: no model was used.', () =>
  withPlugin({}, async () => {
    const script = runById('a-5e6f7a8b')
    const hermes = runById('a-9c0d1e2f')

    assert.equal(script.mode, 'script')
    assert.ok(allText(await mountComponent(C.AgentRunCard, { run: script })).includes('Handoff test: no model was used.'))
    assert.ok(!allText(await mountComponent(C.AgentRunCard, { run: hermes })).includes('no model was used'))
    assert.ok(!allText(await mountComponent(C.AgentRunCard, { run: { ...script, status: 'running', instance_id: 'i-ab12cd34' } })).includes('no model was used'), 'not while it runs')
  })
)

await check('the delete dialog that could not save says its advice once', () =>
  withPlugin(
    { 'POST /instances/i-ab12cd34/destroy': { status: 409, body: { error: { code: 'not_saved', message: 'Files could not be saved, so nothing was deleted: disk full. Delete without saving to remove it anyway.' } } } },
    async () => {
      let tree = await mountComponent(C.DeleteDialog, { instance: PLAIN, onClose() {} })

      tree = await click(findButtons(tree).byLabel('Delete'))

      const text = allText(tree)
      const count = re => (text.match(re) || []).length

      assert.equal(count(/delete without saving to remove it anyway/gi), 1, text)
      assert.equal(count(/nothing was deleted/gi), 1, text)
      assert.ok(!text.includes('use the button below'), text)
      assert.ok(buttonsOf(tree).includes('Delete without saving'), 'the button is still offered')
    }
  )
)

await check('a not_saved message without advice still gets the one sentence', () =>
  withPlugin({ 'POST /instances/i-ab12cd34/destroy': { status: 409, body: { error: { code: 'not_saved', message: 'Files could not be saved: disk full.' } } } }, async () => {
    let tree = await mountComponent(C.DeleteDialog, { instance: PLAIN, onClose() {} })

    tree = await click(findButtons(tree).byLabel('Delete'))
    assert.ok(allText(tree).includes('Nothing was deleted. To remove it anyway'), allText(tree))
  })
)

await check('a stopped environment card has no Terminal, Screen, Forward port or idle-timeout line', () =>
  withPlugin({}, async () => {
    for (const status of ['stopped', 'exited']) {
      const tree = await mountComponent(C.InstanceCard, { instance: { ...STOPPED, status, idle_minutes: 60 } })
      const labels = findButtons(tree).labels()

      for (const gone of ['Terminal', 'Screen', 'Forward port\u2026']) assert.ok(!labels.includes(gone), `${status}: unexpected ${gone}`)
      for (const kept of ['Start', 'Logs', 'Files\u2026', 'Delete']) assert.ok(labels.includes(kept), `${status}: missing ${kept} (have ${labels})`)
      assert.ok(!allText(tree).includes('Stops after'), `${status}: ` + allText(tree))
    }

    const running = await mountComponent(C.InstanceCard, { instance: PLAIN })

    assert.ok(allText(running).includes('Stops after 60 min idle'), 'a running card keeps its idle line')
    assert.ok(findButtons(running).labels().includes('Terminal'))
  })
)

await check('item 12: the Slurm work directory help tells the user to pick shared storage', () =>
  withPlugin({}, async () => {
    const sentence = 'The folder must be on storage the compute nodes also see (shared storage).'

    assert.ok(T.COPY.wizard_hints.workdir.includes(sentence))

    let tree = await mountComponent(C.AddHostWizard, { initial: { kind: 'slurm' }, onFinished() {} })

    tree = await click(findButtons(tree).byLabel('Next'))
    assert.ok(allText(tree).includes(sentence), 'the wizard shows it under Work directory')
  })
)

// =============================================================================
// Slurm output notes, delete, clear finished, work directory
// =============================================================================
const jobRow = (tree, job) => rowOf(tree, job.name, jobsFixture.map(j => j.name).filter(n => n !== job.name))
const JOB_ID_RE = id => new RegExp(`^/jobs/${id}$`)

await check('an empty job output shows the backend note and status, not "No output yet."', async () => {
  const cases = [
    ['missing', 'No output file here', 'There is no output file in /root/cloud-compute/j-1a2b3c4d on slurm-x1. Use a shared work directory.'],
    ['not_started', 'Not started yet', 'The job has not started, so it has written nothing yet.'],
    ['empty', 'Printed nothing', 'The job finished and its output file is empty.']
  ]

  for (const [status, label, note] of cases) {
    await withPlugin({ 'GET /jobs/j-1a2b3c4d/output': { body: { text: '', status, note } } }, async () => {
      let tree = await mountComponent(C.JobsTab)

      tree = await click(findButtons(jobRow(tree, RUNNING_JOB)).byLabel('Output'))

      const text = allText(tree)

      assert.ok(text.includes(note), `${status}: the note must be shown (text: ${text.slice(0, 300)})`)
      assert.ok(text.includes(label), `${status}: the status label "${label}" must be shown`)
      assert.ok(!text.includes('No output yet.'), `${status}: the bare fallback must not replace the note`)
    })
  }
})

await check('output text still shows when present; with no note the plain fallback remains', () =>
  withPlugin({}, async () => {
    let tree = await mountComponent(C.JobsTab)

    tree = await click(findButtons(jobRow(tree, RUNNING_JOB)).byLabel('Output'))
    assert.ok(allText(tree).includes('epoch 2/3 loss=0.64'))
    assert.ok(!allText(tree).includes('No output file here'))
  }).then(() =>
    withPlugin({ 'GET /jobs/j-1a2b3c4d/output': { body: { text: '', status: 'empty', note: null } } }, async () => {
      let tree = await mountComponent(C.JobsTab)

      tree = await click(findButtons(jobRow(tree, RUNNING_JOB)).byLabel('Output'))
      assert.ok(allText(tree).includes('No output yet.'))
    })
  )
)

await check('the environment Logs dialog shows the backend note for empty logs', () =>
  withPlugin({ 'GET /instances/i-ab12cd34/logs': { body: { text: '', note: 'The container has printed nothing yet (sleep infinity prints nothing).' } } }, async () => {
    let tree = await mountComponent(C.InstanceCard, { instance: PLAIN })

    tree = await click(findButtons(tree).byLabel('Logs'))
    assert.ok(allText(tree).includes('The container has printed nothing yet'), allText(tree).slice(0, 300))
  })
)

await check('jobActions offers delete only for finished jobs', () => {
  for (const state of ['COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'OUT_OF_MEMORY', 'NODE_FAIL']) {
    assert.ok(T.jobActions({ id: 'j-00000000', state }).includes('delete'), `${state} can be removed`)
  }

  for (const state of ['PENDING', 'RUNNING', 'COMPLETING']) {
    assert.ok(!T.jobActions({ id: 'j-00000000', state }).includes('delete'), `${state} cannot be removed`)
  }

  assert.ok(!T.jobActions({ id: 'j-00000000', state: 'UNKNOWN', ended_at: null }).includes('delete'))
  assert.ok(T.jobActions({ id: 'j-00000000', state: 'UNKNOWN', ended_at: '2026-11-08T08:03:02Z' }).includes('delete'))
})

await check('Remove from list asks first, says the cluster folder stays, then DELETE /jobs/{id}', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.JobsTab)

    tree = await click(findButtons(jobRow(tree, DONE_JOB)).byLabel('Remove from list'))
    assert.ok(!fake.restCalls.some(c => c.method === 'DELETE'), 'nothing is sent before the confirmation')

    const dialog = openConfirm(tree)

    assert.ok(dialog, 'a ConfirmDialog must open')
    assert.ok(/only removes the record/.test(dialog.props.description), dialog.props.description)
    assert.ok(/on the cluster stays/.test(dialog.props.description), dialog.props.description)
    assert.ok(dialog.props.description.includes(DONE_JOB.job_dir), 'the job folder is named')
    assert.equal(dialog.props.destructive, true)
    await dialog.props.onConfirm()
    await dialog.session.settle()

    const dels = fake.restCalls.filter(c => c.method === 'DELETE' && JOB_ID_RE(DONE_JOB.id).test(c.pathname))

    assert.equal(dels.length, 1, `exactly one DELETE /jobs/${DONE_JOB.id} (calls: ${restLines(fake)})`)
    assert.ok(toastTexts().some(t => t.includes('was not deleted')), `the backend note is shown: ${JSON.stringify(toastTexts())}`)
  })
)

await check('a 409 for an unfinished job is shown as plain text', () =>
  withPlugin({ 'DELETE /jobs/j-99aa11bb': { status: 409, body: { error: { code: 'conflict', message: 'job j-99aa11bb has not finished (RUNNING); cancel it first' } } } }, async (ctx, fake) => {
    let tree = await mountComponent(C.JobsTab)

    tree = await click(findButtons(jobRow(tree, DONE_JOB)).byLabel('Remove from list'))

    const dialog = openConfirm(tree)
    let message = ''

    try {
      await dialog.props.onConfirm()
    } catch (error) {
      message = error.message
    }

    assert.equal(message, 'job j-99aa11bb has not finished (RUNNING); cancel it first')
  })
)

await check('Clear finished counts finished jobs, confirms, then POST /jobs/clear-finished', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.JobsTab)
    const finished = jobsFixture.filter(j => T.jobActions(j).includes('delete')).length

    assert.ok(finished >= 2, 'fixtures hold several finished jobs')
    assert.ok(findButtons(tree).labels().includes('Clear finished\u2026'), `buttons: ${findButtons(tree).labels()}`)
    tree = await click(findButtons(tree).byLabel('Clear finished\u2026'))
    assert.ok(!fake.restCalls.some(c => c.pathname === '/jobs/clear-finished'), 'nothing is sent before the confirmation')

    const dialog = openConfirm(tree)

    assert.ok(dialog.props.title.includes(String(finished)), dialog.props.title)
    assert.ok(/on this computer/.test(dialog.props.description) && /on the cluster stay/.test(dialog.props.description), dialog.props.description)
    assert.ok(/still pending or running are not touched/.test(dialog.props.description))
    await dialog.props.onConfirm()
    await dialog.session.settle()
    assert.equal(fake.restCalls.filter(c => c.method === 'POST' && c.pathname === '/jobs/clear-finished').length, 1)
    assert.ok(toastTexts().some(t => t.includes('Removed 2 finished jobs')), JSON.stringify(toastTexts()))
  })
)

await check('no Clear finished button while every job is still active', () =>
  withPlugin({ 'GET /jobs': { body: jobsFixture.filter(j => !T.jobActions(j).includes('delete')) } }, async () => {
    const tree = await mountComponent(C.JobsTab)

    assert.ok(!findButtons(tree).labels().includes('Clear finished\u2026'))
  })
)

await check('the connection checklist labels the workdir step "Work directory"', () => {
  assert.equal(T.STEP_LABELS.workdir, 'Work directory')
})

await check('the host card shows a failed work-directory check with its fix', () =>
  withPlugin({}, async () => {
    const item = readFixture('hosts_slurm.json')[0]

    item.state.capabilities.workdir_shared = false
    item.state.capabilities.workdir_detail = '/root/cloud-compute is writable but not visible from c1. Fix: use a folder on shared storage, for example /data/cloud-compute'

    const text = allText(await mountComponent(C.HostCard, { item }))

    assert.ok(text.includes('Work directory check failed: /root/cloud-compute is writable but not visible from c1'), text.slice(0, 400))
    assert.ok(text.includes('Fix: use a folder on shared storage'), 'the fix hint is shown')
    assert.ok(text.includes('Change the Work directory in Edit'), 'and where to change it')

    const ok = readFixture('hosts_slurm.json')[0]

    ok.state.capabilities.workdir_shared = true
    ok.state.capabilities.workdir_detail = 'fine'
    assert.ok(!allText(await mountComponent(C.HostCard, { item: ok })).includes('Work directory check failed'))
  })
)

await check('the Test connection dialog names the workdir row and shows its fix', () =>
  withPlugin({
    'POST /hosts/test': {
      body: {
        ok: false,
        status: 'degraded',
        steps: [{ name: 'workdir', ok: false, detail: '/root/cloud-compute is not visible from c1', hint: 'Use a folder on shared storage', ms: 20 }],
        capabilities: null
      }
    }
  }, async () => {
    let tree = await mountComponent(C.HostCard, { item: readFixture('hosts_slurm.json')[0] })

    tree = await click(findButtons(tree).byLabel('Test connection'))

    const text = allText(tree)

    assert.ok(text.includes('Work directory'), text.slice(0, 400))
    assert.ok(text.includes('Fix: Use a folder on shared storage'))
  })
)

await check('the Slurm Work directory help says shared storage and that Test connection checks it', () =>
  withPlugin({}, async () => {
    const hint = T.COPY.wizard_hints.workdir

    assert.ok(hint.includes('The folder must be on storage the compute nodes also see (shared storage).'))
    assert.ok(hint.includes('Test connection checks this.'))
  })
)

// =============================================================================
// cloud agents (reply, follow-up, wait, model choice, failure, remove)
// =============================================================================
const LONG_REPLY = Array.from({ length: 30 }, (_, i) => `line ${i + 1} of the answer`).join('\n')
const doneRun = (extra = {}) => ({ ...runById('a-5e6f7a8b'), mode: 'hermes', template_id: 'hermes-agent', status: 'done', ...extra })
const sandboxList = run => [{ ...PLAIN, id: run.instance_id }]
const SAVED_MODEL = readFixture('agent_models.json').models[0]

await check('the run card shows the reply, the turn count, the model and the endpoint host', () =>
  withPlugin({}, async () => {
    const run = doneRun({ reply: 'Created NOTES.md and committed it.', turns: 2, model: 'deepseek-ai/DeepSeek-V4.1-Flash-Fast', endpoint_host: 'inference.baseten.co' })
    const text = allText(await mountComponent(C.AgentRunCard, { run, instances: sandboxList(run) }))

    assert.ok(text.includes("Agent's reply") && text.includes('Created NOTES.md and committed it.'), text.slice(0, 400))
    assert.ok(text.includes('2 turns'), 'turn count')
    assert.ok(text.includes('Model deepseek-ai/DeepSeek-V4.1-Flash-Fast at inference.baseten.co'), 'model and endpoint host')
  })
)

await check('a long reply is cut and Show all reveals the rest', () =>
  withPlugin({}, async () => {
    const run = doneRun({ reply: LONG_REPLY })
    let tree = await mountComponent(C.AgentRunCard, { run, instances: sandboxList(run) })

    assert.ok(allText(tree).includes('line 1 of the answer') && !allText(tree).includes('line 30 of the answer'), 'collapsed')
    tree = await click(findButtons(tree).byLabel('Show all'))
    assert.ok(allText(tree).includes('line 30 of the answer'), 'expanded')
    tree = await click(findButtons(tree).byLabel('Show less'))
    assert.ok(!allText(tree).includes('line 30 of the answer'), 'collapsed again')
  })
)

await check('a running run says it is waiting, updates by itself and hides the old reply', () =>
  withPlugin({}, async () => {
    const run = doneRun({ status: 'running', reply: 'the earlier answer', turns: 2, ended_at: null })
    const text = allText(await mountComponent(C.AgentRunCard, { run, instances: sandboxList(run) }))

    assert.ok(text.includes("Waiting for the agent's reply"), text.slice(0, 400))
    assert.ok(text.includes('updates by itself'))
    assert.ok(text.includes('Working on your message (turn 2)'))
    assert.ok(!text.includes('the earlier answer'), 'the old reply is not shown as the answer')
  })
)

await check('the follow-up box posts the message and clears; the card then refetches', () =>
  withPlugin({}, async (ctx, fake) => {
    const run = doneRun({ reply: 'ok' })
    let tree = await mountComponent(C.AgentRunCard, { run, instances: sandboxList(run) })

    assert.ok(allText(tree).includes('Send a message to this agent'))
    assert.ok(findButtons(tree).byLabel('Send message').props.disabled, 'Send is off while the box is empty')
    tree = await setValue(inputField(tree, ['send a message to this agent'], 'AgentRunCard'), 'Also add a test.')
    tree = await click(findButtons(tree).byLabel('Send message'))

    const posts = callsMatching(fake, 'POST', /^\/agents\/a-5e6f7a8b\/message$/)

    assert.equal(posts.length, 1)
    assert.deepEqual(bodyOf(posts[0]), { message: 'Also add a test.' })
    assert.ok(toastTexts().some(t => t.includes('Message sent to a-5e6f7a8b')), JSON.stringify(toastTexts()))
    assert.equal(inputField(tree, ['send a message to this agent'], 'AgentRunCard').props.value, '', 'the box is cleared')
  })
)

await check('the follow-up box is disabled with a reason while the run works, when stopped or when its sandbox is gone', () =>
  withPlugin({}, async () => {
    const live = doneRun({ status: 'running' })
    let tree = await mountComponent(C.AgentRunCard, { run: live, instances: sandboxList(live) })

    assert.ok(inputField(tree, ['send a message to this agent'], 'AgentRunCard').props.disabled)
    assert.ok(allText(tree).includes('still working on its turn'))

    const gone = doneRun({ instance_id: 'i-deadbeef' })

    tree = await mountComponent(C.AgentRunCard, { run: gone, instances: [PLAIN] })
    assert.ok(inputField(tree, ['send a message to this agent'], 'AgentRunCard').props.disabled)
    assert.ok(allText(tree).includes('sandbox is gone'), allText(tree).slice(0, 500))
    assert.ok(findButtons(tree).byLabel('Send message').props.disabled)
  })
)

await check('a 409 from the message route is shown as text', () =>
  withPlugin({ 'POST /agents/a-5e6f7a8b/message': { status: 409, body: { error: { code: 'conflict', message: 'cloud agent run a-5e6f7a8b is still working on its turn.' } } } }, async () => {
    const run = doneRun()
    let tree = await mountComponent(C.AgentRunCard, { run, instances: sandboxList(run) })

    tree = await setValue(inputField(tree, ['send a message to this agent'], 'AgentRunCard'), 'More please')
    tree = await click(findButtons(tree).byLabel('Send message'))
    assert.ok(allText(tree).includes('is still working on its turn.'), allText(tree).slice(0, 500))
  })
)

const MODELS_WITH_EXTRAS = {
  models: [SAVED_MODEL],
  detected: [{ name: 'Hermes main model', base_url: 'https://llm.example.net/v1', model: 'qwen3-coder', key_secret: 'MY_LLM_KEY', endpoint_host: 'llm.example.net' }],
  unavailable: [{ name: 'Claude (subscription sign-in)', reason: 'Uses a sign-in, not an API key, so it cannot run in a sandbox.' }],
  default_id: SAVED_MODEL.id
}

await check('the New agent run dialog lists saved, detected and unavailable models without leaving the dialog', () =>
  withPlugin({ 'GET /agent-models': { body: MODELS_WITH_EXTRAS } }, async () => {
    let tree = await mountComponent(C.AgentsTab)

    tree = await click(findButtons(tree).byLabel('New agent run\u2026'))

    const dialog = () => openDialog(tree, 'NewAgentDialog')
    const text = allText(dialog())

    assert.ok(text.includes('Default model') && text.includes(SAVED_MODEL.name), text.slice(0, 600))
    assert.ok(text.includes('Hermes main model') && text.includes('from this Hermes profile'))
    assert.ok(text.includes('Claude (subscription sign-in)') && text.includes('Uses a sign-in, not an API key'), 'the reason is shown')

    const rows = findButtons(dialog())
    const unavailable = rows.find(b => b.label.includes('Claude (subscription sign-in)'))

    assert.ok(unavailable.props.disabled, 'the unavailable entry is greyed out and cannot be picked')
    assert.ok(rows.labels().some(l => l.includes('Another endpoint')))
    assert.ok(!findButtons(dialog()).labels().includes('Open settings'), 'no trip to Settings is needed when a key is set')
  })
)

const SECOND_MODEL = { ...SAVED_MODEL, id: 'm-22223333', name: 'Second choice', model: 'second-model', is_default: false }

await check('picking a saved model sends its id and keeps the choice in the draft', () =>
  withPlugin({ 'GET /agent-models': { body: { ...MODELS_WITH_EXTRAS, models: [...MODELS_WITH_EXTRAS.models, SECOND_MODEL] } } }, async (ctx, fake) => {
    let tree = await mountComponent(C.AgentsTab)

    tree = await click(findButtons(tree).byLabel('New agent run\u2026'))
    tree = await click(findButtons(openDialog(tree, 'NewAgentDialog')).find(b => b.label.includes('Second choice') && b.label.includes('saved')))
    assert.equal(draftOf(fake).model, `m:${SECOND_MODEL.id}`, 'the choice is in the draft')
    tree = await setValue(inputField(openDialog(tree, 'NewAgentDialog'), ['task for the agent'], 'NewAgentDialog'), 'Describe this repo')
    tree = await setValue(inputField(openDialog(tree, 'NewAgentDialog'), ['repository path on this computer'], 'NewAgentDialog'), '/home/dev/code/demo')
    tree = await click(findButtons(openDialog(tree, 'NewAgentDialog')).byLabel('Start'))

    const body = bodyOf(callsMatching(fake, 'POST', /^\/agents$/)[0])

    assert.equal(body.model_id, SECOND_MODEL.id)
    assert.equal(callsMatching(fake, 'POST', /^\/agent-models$/).length, 0, 'a saved model is not saved again')
  })
)

await check('a model from this Hermes profile is saved first, then used by id', () =>
  withPlugin({ 'GET /agent-models': { body: MODELS_WITH_EXTRAS }, 'POST /agent-models': { body: { ...SAVED_MODEL, id: 'm-0a0b0c0d', name: 'Hermes main model' } } }, async (ctx, fake) => {
    let tree = await mountComponent(C.AgentsTab)

    tree = await click(findButtons(tree).byLabel('New agent run\u2026'))
    tree = await click(findButtons(openDialog(tree, 'NewAgentDialog')).find(b => b.label.includes('Hermes main model')))
    tree = await setValue(inputField(openDialog(tree, 'NewAgentDialog'), ['task for the agent'], 'NewAgentDialog'), 'Describe this repo')
    tree = await setValue(inputField(openDialog(tree, 'NewAgentDialog'), ['repository path on this computer'], 'NewAgentDialog'), '/home/dev/code/demo')
    tree = await click(findButtons(openDialog(tree, 'NewAgentDialog')).byLabel('Start'))

    const saves = callsMatching(fake, 'POST', /^\/agent-models$/)

    assert.equal(saves.length, 1)
    assert.deepEqual(bodyOf(saves[0]), { name: 'Hermes main model', base_url: 'https://llm.example.net/v1', model: 'qwen3-coder', key_secret: 'MY_LLM_KEY' })
    assert.equal(bodyOf(callsMatching(fake, 'POST', /^\/agents$/)[0]).model_id, 'm-0a0b0c0d')
  })
)

await check('Another endpoint: typed endpoint and model, Save this choice, validation, and the draft keeps them', () =>
  withPlugin({ 'POST /agent-models': { body: { ...SAVED_MODEL, id: 'm-1a1b1c1d', name: 'qwen3-coder at llm.example.net' } } }, async (ctx, fake) => {
    let tree = await mountComponent(C.AgentsTab)

    tree = await click(findButtons(tree).byLabel('New agent run\u2026'))
    tree = await click(findButtons(openDialog(tree, 'NewAgentDialog')).find(b => b.label.includes('Another endpoint')))
    assert.ok(findButtons(openDialog(tree, 'NewAgentDialog')).labels().includes('Save this choice'))
    tree = await click(findButtons(openDialog(tree, 'NewAgentDialog')).byLabel('Save this choice'))
    assert.ok(allText(tree).includes('Enter the model endpoint as an http or https address'), 'an empty endpoint is refused in plain words')
    assert.equal(callsMatching(fake, 'POST', /^\/agent-models$/).length, 0)
    tree = await setValue(inputField(openDialog(tree, 'NewAgentDialog'), ['endpoint'], 'NewAgentDialog'), 'https://llm.example.net/v1')
    tree = await setValue(inputField(openDialog(tree, 'NewAgentDialog'), ['model id'], 'NewAgentDialog'), 'qwen3-coder')
    assert.equal(draftOf(fake).ep_url, 'https://llm.example.net/v1', 'typed text is in the draft')
    tree = await click(findButtons(openDialog(tree, 'NewAgentDialog')).byLabel('Save this choice'))

    const saves = callsMatching(fake, 'POST', /^\/agent-models$/)

    assert.equal(saves.length, 1)
    assert.deepEqual(bodyOf(saves[0]), { base_url: 'https://llm.example.net/v1', model: 'qwen3-coder' })
    assert.equal(draftOf(fake).model, 'm:m-1a1b1c1d', 'the saved choice is selected')
    assert.equal(draftOf(fake).ep_url, '', 'the typed fields are cleared after saving')
  })
)

await check('a saved model whose key is not set says so and links to Settings; the handoff test shows no model picker', () =>
  withPlugin({ 'GET /agent-models': { body: { ...MODELS_WITH_EXTRAS, models: [{ ...SAVED_MODEL, key_set: false }] } }, 'GET /templates': { body: withScriptTemplate() } }, async () => {
    let tree = await mountComponent(C.AgentsTab)

    tree = await click(findButtons(tree).byLabel('New agent run\u2026'))
    assert.ok(allText(openDialog(tree, 'NewAgentDialog')).includes('is not set yet. Add it in Settings; this draft is kept.'))

    const pick = findAll(openDialog(tree, 'NewAgentDialog'), n => n.name === 'Pick').find(n => (n.props.options || []).some(o => o.value === 'agent-script'))

    tree = await setValue(pick, 'agent-script')
    assert.ok(!allText(openDialog(tree, 'NewAgentDialog')).includes('Another endpoint'), 'no model for a handoff test')
  })
)

await check('a failed run shows the cause and the next step; a backend next step wins', () =>
  withPlugin({}, async () => {
    const failed = { ...runById('a-9c0d1e2f'), error: 'The model answered HTTP 404: please check the model you provided.' }
    let text = allText(await mountComponent(C.AgentRunCard, { run: failed, instances: sandboxList(failed) }))

    assert.ok(text.includes('The run failed') && text.includes('please check the model you provided'), text.slice(0, 500))
    assert.ok(text.includes('What to do: Read the cause above. Press Run again'), 'the default next step')
    text = allText(await mountComponent(C.AgentRunCard, { run: { ...failed, next: 'Change the model id in Settings, then run again.' }, instances: sandboxList(failed) }))
    assert.ok(text.includes('What to do: Change the model id in Settings, then run again.'))
  })
)

await check('Run again posts to the rerun route with only what changed', () =>
  withPlugin({ 'GET /agent-models': { body: { ...MODELS_WITH_EXTRAS, models: [SAVED_MODEL, { ...SAVED_MODEL, id: 'm-2b2b2b2b', name: 'Other model' }] } } }, async (ctx, fake) => {
    const failed = { ...runById('a-9c0d1e2f'), model_id: SAVED_MODEL.id }
    let tree = await mountComponent(C.AgentRunCard, { run: failed, instances: sandboxList(failed), hosts: readFixture('hosts.json') })

    tree = await click(findButtons(tree).byLabel('Run again\u2026'))
    assert.ok(openDialog(tree, 'RerunDialog'), 'a small dialog opens first')
    assert.ok(allText(openDialog(tree, 'RerunDialog')).includes('Other model'), 'the model can be changed')
    tree = await click(findButtons(openDialog(tree, 'RerunDialog')).find(b => b.label.includes('Other model')))
    tree = await click(findButtons(openDialog(tree, 'RerunDialog')).byLabel('Run again'))

    const posts = callsMatching(fake, 'POST', /^\/agents\/a-9c0d1e2f\/rerun$/)

    assert.equal(posts.length, 1)
    assert.deepEqual(bodyOf(posts[0]), { model_id: 'm-2b2b2b2b' })
    assert.ok(toastTexts().some(t => t.includes('Started cloud agent a-7d8e9f0a')), JSON.stringify(toastTexts()))
  })
)

await check('without the rerun route (404) Run again falls back to the prefilled New agent run form', () =>
  withPlugin({ 'POST /agents/a-9c0d1e2f/rerun': { status: 404, body: { error: { code: 'not_found', message: 'Not Found' } } } }, async (ctx, fake) => {
    let tree = await mountComponent(C.AgentsTab)
    const failed = runById('a-9c0d1e2f')

    tree = await click(findButtons(cardOf(tree, failed.id)).byLabel('Run again\u2026'))
    tree = await click(findButtons(openDialog(tree, 'RerunDialog')).byLabel('Run again'))

    const dialog = openDialog(tree, 'NewAgentDialog')

    assert.ok(dialog, 'the form opens')
    assert.equal(fieldValue(dialog, ['task for the agent'], 'NewAgentDialog'), failed.goal)
  })
)

await check('Remove asks, says only the record goes, then DELETE /agents/{id}', () =>
  withPlugin({}, async (ctx, fake) => {
    const failed = { ...runById('a-9c0d1e2f') }
    let tree = await mountComponent(C.AgentRunCard, { run: failed, instances: [PLAIN] })

    tree = await click(findButtons(tree).byLabel('Remove'))
    assert.equal(callsMatching(fake, 'DELETE', /^\/agents\//).length, 0)

    const dialog = openConfirm(tree)

    assert.ok(/Only the record of this run is removed/.test(dialog.props.description), dialog.props.description)
    await dialog.props.onConfirm()
    await dialog.session.settle()
    assert.equal(callsMatching(fake, 'DELETE', /^\/agents\/a-9c0d1e2f$/).length, 1)
    assert.ok(toastTexts().some(t => t.includes('Only the run record was removed')), JSON.stringify(toastTexts()))
  })
)

await check('Remove is off with the backend rule while the run is active or its sandbox still exists', () =>
  withPlugin({}, async () => {
    const live = doneRun({ status: 'running' })
    let tree = await mountComponent(C.AgentRunCard, { run: live, instances: sandboxList(live) })

    assert.ok(findButtons(tree).byLabel('Remove') === undefined || findButtons(tree).byLabel('Remove').props.disabled)
    assert.ok(allText(tree).includes('An active run must be stopped first'))

    const done = doneRun()

    tree = await mountComponent(C.AgentRunCard, { run: done, instances: sandboxList(done) })
    assert.ok(allText(tree).includes('Stop the run first. Stop saves the files in /work to this computer'), allText(tree).slice(0, 600))
  })
)

// =============================================================================
// saved copies, size before saving, destination folder
// =============================================================================
const DESTROYED = readFixture('instance_destroyed.json')
const DESTROYED_EMPTY = readFixture('instance_destroyed_empty.json')
const saveDialog = tree => openDialog(tree, 'SaveDialog')

await check('Delete shows the backend message, the saved path, the file count and size, with Copy and Show in folder', () =>
  withPlugin({ 'POST /instances/i-ab12cd34/destroy': { fixture: 'instance_destroyed.json' } }, async (ctx, fake) => {
    T.$deleted.set(null)

    let tree = await mountComponent(C.DeleteDialog, { instance: PLAIN, onClose() {} })

    tree = await click(findButtons(tree).byLabel('Delete'))

    const shown = T.$deleted.get()

    assert.equal(shown.message, DESTROYED.message)
    assert.equal(shown.export.id, 'x-5e6f7a8b')
    assert.ok(toastTexts().some(t => t.includes('(8 files, 1.9 MB) were saved to')), JSON.stringify(toastTexts()))

    const notice = await mountComponent(C.DeletedNotice)
    const text = allText(notice)

    assert.ok(text.includes(DESTROYED.message), 'the backend message is the description')
    assert.ok(text.includes(DESTROYED.saved.path), 'the saved path')
    assert.ok(text.includes('1.9 MB, 8 files'), `size and file count: ${text}`)
    assert.ok(buttonsOf(notice).includes('Show in folder'), 'Show in folder where the platform allows it')
    await click(findButtons(notice).byLabel('Show in folder'))
    assert.deepEqual(ctx.fake.osCalls.revealPath, [DESTROYED.saved.path])
  })
)

await check('Delete with nothing to save still says so, never just "deleted"', () =>
  withPlugin({ 'POST /instances/i-ab12cd34/destroy': { fixture: 'instance_destroyed_empty.json' } }, async () => {
    T.$deleted.set(null)

    let tree = await mountComponent(C.DeleteDialog, { instance: PLAIN, onClose() {} })

    tree = await click(findButtons(tree).byLabel('Delete'))
    assert.ok(toastTexts().some(t => t.includes('Nothing in /work needed saving.')), JSON.stringify(toastTexts()))

    const notice = await mountComponent(C.DeletedNotice)

    assert.ok(allText(notice).includes('python-dev was deleted') && allText(notice).includes('Nothing in /work needed saving.'), allText(notice))
    assert.ok(allText(notice).includes('No copy was made'), 'and that nothing can be restored')
    T.$deleted.set(null)
  })
)

await check('Show in folder is absent where the platform cannot reveal a path', () =>
  withPlugin({}, async (ctx) => {
    delete ctx.os.revealPath
    T.$deleted.set({ name: 'python-dev', instance_id: 'i-ab12cd34', message: DESTROYED.message, export: FETCHED })

    const notice = await mountComponent(C.DeletedNotice)

    assert.ok(!buttonsOf(notice).includes('Show in folder'))
    assert.ok(buttonsOf(notice).includes('Close'))
    T.$deleted.set(null)
  })
)

await check('Save all measures first and shows "<size>, <n> files" before anything is transferred', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.FilesDialog, { instance: PLAIN, onClose() {} })

    tree = await click(findButtons(tree).byLabel('Save all of /work'))
    assert.deepEqual(callsTo(fake, 'GET', '/instances/i-ab12cd34/files/size').map(c => c.query.path), ['/work'])
    assert.equal(callsTo(fake, 'POST', '/instances/i-ab12cd34/files/fetch').length, 0, 'nothing is sent yet')
    assert.ok(allText(saveDialog(tree)).includes('/work holds 2 MB, 12 files'), allText(saveDialog(tree)))
    assert.ok(buttonsOf(saveDialog(tree)).includes('Save 2 MB, 12 files'))
    tree = await click(findButtons(saveDialog(tree)).byLabel('Cancel'))
    assert.equal(callsTo(fake, 'POST', '/instances/i-ab12cd34/files/fetch').length, 0, 'Cancel sends nothing')
  })
)

await check('a size over the threshold or an unknown size is said plainly and the button reads Save anyway', async () => {
  await withPlugin({ 'GET /instances/i-ab12cd34/files/size': { body: { path: '/work', known: true, bytes: 5 * 1024 * 1024 * 1024, files: 4000, reason: null } } }, async () => {
    let tree = await mountComponent(C.FilesDialog, { instance: PLAIN, onClose() {} })

    tree = await click(findButtons(tree).byLabel('Save all of /work'))
    assert.ok(allText(saveDialog(tree)).includes('This is a large transfer (more than 200 MB)'), allText(saveDialog(tree)))
    assert.ok(allText(saveDialog(tree)).includes('5 GB, 4000 files'))
    assert.ok(buttonsOf(saveDialog(tree)).includes('Save anyway'))
  })
  await withPlugin({ 'GET /instances/i-ab12cd34/files/size': { body: { path: '/work', known: false, bytes: null, files: null, reason: 'Measuring /work in python-dev took longer than 20 seconds.' } } }, async () => {
    let tree = await mountComponent(C.FilesDialog, { instance: PLAIN, onClose() {} })

    tree = await click(findButtons(tree).byLabel('Save all of /work'))
    assert.ok(allText(saveDialog(tree)).includes('The size of /work is not known'))
    assert.ok(allText(saveDialog(tree)).includes('took longer than 20 seconds'), 'the backend reason')
    assert.ok(allText(saveDialog(tree)).includes('no way to say how much will be transferred'))
    assert.ok(buttonsOf(saveDialog(tree)).includes('Save anyway'))
  })
})

await check('a folder without files is information (no Save button); a 409 nothing_to_save is not an error', async () => {
  await withPlugin({ 'GET /instances/i-ab12cd34/files/size': { body: { path: '/work', known: true, bytes: 0, files: 0, reason: null } } }, async () => {
    let tree = await mountComponent(C.FilesDialog, { instance: PLAIN, onClose() {} })

    tree = await click(findButtons(tree).byLabel('Save all of /work'))
    assert.ok(allText(saveDialog(tree)).includes('Nothing to save: there are no files in it, hidden files included'))
    assert.ok(!buttonsOf(saveDialog(tree)).some(l => l.startsWith('Save ')) || buttonsOf(saveDialog(tree)).every(l => !l.startsWith('Save 0') && l !== 'Save anyway'), 'no Save button')
  })
  await withPlugin({ 'POST /instances/i-ab12cd34/files/fetch': { status: 409, body: { error: { code: 'nothing_to_save', message: 'Nothing to save: /work in python-dev has no files.' } } } }, async () => {
    let tree = await mountComponent(C.FilesDialog, { instance: PLAIN, onClose() {} })

    tree = await click(findButtons(tree).byLabel('Save all of /work'))
    tree = await click(findButtons(tree).byLabel('Save 2 MB, 12 files'))

    const text = allText(saveDialog(tree))

    assert.ok(text.includes('Nothing to save: /work in python-dev has no files.'), text)
    assert.ok(!findAll(saveDialog(tree), n => n.name === 'ErrorLine' && n.props.message).length, 'shown as information, not as an error')
  })
})

await check('Save into folder and Unpack into a new subfolder are sent only when used; extracted_to is shown', () =>
  withPlugin({ 'POST /instances/i-ab12cd34/files/fetch': { body: { ...FETCHED, extracted_to: 'C:/Users/me/project/saved/python-dev-20261108T143000Z', message: 'Left out of the copy: the agent-internal folders /work/.hermes and /work/.cc-agent.' } } }, async (ctx, fake) => {
    let tree = await mountComponent(C.FilesDialog, { instance: PLAIN, onClose() {} })

    tree = await click(findButtons(tree).byLabel('Save all of /work'))
    assert.ok(allText(saveDialog(tree)).includes('Blank saves into ' + EXPORTS.folder), 'the default folder is named')
    tree = await setValue(inputField(saveDialog(tree), ['save into folder (optional)'], 'SaveDialog'), '"C:/Users/me/project/saved"')
    tree = await setValue(findAll(saveDialog(tree), n => n.name === 'Checkbox')[0], true)
    tree = await click(findButtons(saveDialog(tree)).byLabel('Save 2 MB, 12 files'))
    assert.deepEqual(bodyOf(callsTo(fake, 'POST', '/instances/i-ab12cd34/files/fetch')[0]), { path: '/work', dest_dir: 'C:/Users/me/project/saved', extract: true })

    const text = allText(saveDialog(tree))

    assert.ok(text.includes('Also unpacked into this new folder') && text.includes('C:/Users/me/project/saved/python-dev-20261108T143000Z'), text)
    assert.ok(text.includes('Left out of the copy: the agent-internal folders /work/.hermes and /work/.cc-agent.'), 'sentence about agent-internal folders')
    assert.ok(text.includes('12 files'), 'file count')
  })
)

await check('a saved copy with an unpacked folder says the folder stays when the copy is deleted; the backend note is shown', () =>
  withPlugin({
    'GET /exports': { body: { exports: [{ ...EXPORTS.exports[0], extracted_to: 'C:/Users/me/project/saved/python-dev-1' }], folder: EXPORTS.folder } },
    'DELETE /exports/x-5e6f7a8b': { body: { deleted: true, message: 'Deleted the saved copy 20261108T150100Z-before-delete.tar.gz. The unpacked folder C:/Users/me/project/saved/python-dev-1 was left in place.' } }
  }, async (ctx, fake) => {
    let tree = await mountComponent(C.FilesDialog, { instance: PLAIN, onClose() {} })

    assert.ok(allText(tree).includes('stays when this copy is deleted'), 'the row says so')
    tree = await click(findButtons(findAll(tree, n => n.name === 'ExportRow')[0]).byLabel('Delete'))
    assert.ok(textOf(openConfirm(tree)).includes('is not removed'), textOf(openConfirm(tree)))
    tree = await click(findButtons(openConfirm(tree)).byLabel('Delete'))
    assert.ok(toastTexts().some(t => t.includes('was left in place')), JSON.stringify(toastTexts()))
  })
)

// =============================================================================
// screen outcomes, spawn warnings, published ports
// =============================================================================
const PORT_TEMPLATE = readFixture('templates.json').find(t => t.kind === 'container' && !t.agent)
const withPorts = (instance, ports) => ({ ...instance, status: 'running', ports })

await check('a refused Screen shows the backend sentence and points at the templates', () =>
  withPlugin({
    'POST /instances/i-ef56aa01/display': {
      status: 409,
      body: { error: { code: 'needs_setup', message: 'desktop-novnc has no screen: its template uses the image ubuntu:24.04, which has no screen server. Use the Desktop (noVNC) template, or turn off Show a screen in this template.' } }
    }
  }, async (ctx, fake) => {
    let tree = await mountComponent(C.InstanceCard, { instance: { ...DISPLAY, status: 'running' } })

    tree = await click(findButtons(tree).byLabel('Screen'))

    const text = allText(tree)

    assert.ok(text.includes('which has no screen server. Use the Desktop (noVNC) template'), text.slice(0, 500))
    assert.ok(!text.includes('screen not ready after 10 s'))
    assert.ok(buttonsOf(tree).includes('Open Templates'), 'a way to the templates')
    assert.equal(fake.restCalls.filter(c => c.pathname.endsWith('/forward')).length, 0)
  })
)

await check('an unavailable screen shows the sentence without the templates button', () =>
  withPlugin({ 'POST /instances/i-ef56aa01/display': { status: 503, body: { error: { code: 'unavailable', message: 'The screen of desktop-novnc is still starting. Try again in a few seconds.' } } } }, async () => {
    let tree = await mountComponent(C.InstanceCard, { instance: { ...DISPLAY, status: 'running' } })

    tree = await click(findButtons(tree).byLabel('Screen'))
    assert.ok(allText(tree).includes('is still starting. Try again in a few seconds.'))
    assert.ok(!buttonsOf(tree).includes('Open Templates'))
  })
)

await check('warnings from the preview are shown in the Spawn dialog and again after the spawn', () =>
  withPlugin({
    'GET /templates/python-dev/preview': { body: { ...readFixture('template_preview.json'), warnings: ['This image has no screen server, so Show a screen will not work. Use the Desktop (noVNC) template.'] } }
  }, async (ctx, fake) => {
    const hosts = readFixture('hosts.json')
    let tree = await mountComponent(C.SpawnDialog, { templates: TEMPLATES, hosts, template: TEMPLATES.find(t => t.id === 'python-dev'), hostId: 'x1carbon', onClose() {}, onSpawned() {} })

    assert.ok(allText(tree).includes('Heads-up: This image has no screen server'), allText(tree).slice(0, 600))
    tree = await click(findButtons(tree).byLabel('Spawn'))
    assert.ok(toastTexts().some(t => t.includes('Heads-up for') && t.includes('has no screen server')), JSON.stringify(toastTexts()))
  })
)

await check('validatePorts follows the backend rules', () => {
  assert.deepEqual(T.validatePorts('8000, 3000', false), { ports: [8000, 3000] })
  assert.deepEqual(T.validatePorts('', false), { ports: [] })
  assert.deepEqual(T.validatePorts('8000 3000;9000', false), { ports: [8000, 3000, 9000] })
  assert.ok(T.validatePorts('0', false).error.includes('1 to 65535'))
  assert.ok(T.validatePorts('70000', false).error.includes('1 to 65535'))
  assert.ok(T.validatePorts('web', false).error.includes('is not valid'))
  assert.equal(T.validatePorts('8000, 8000', false).error, 'Port 8000 is listed twice.')
  assert.ok(T.validatePorts('6080', true).error.includes('used by the screen'))
  assert.deepEqual(T.validatePorts('6080', false), { ports: [6080] }, '6080 is only reserved when the screen is on')
  assert.ok(T.validatePorts('1,2,3,4,5,6,7,8,9,10,11', false).error.includes('at most 10'))
})

await check('the template editor has Published ports; invalid values are refused before any request; the backend refusal is shown', async () => {
  await withPlugin({}, async (ctx, fake) => {
    let tree = await mountComponent(C.TemplateEditor, { template: { ...PORT_TEMPLATE, ports: [8000] }, onClose() {} })

    assert.equal(fieldValue(tree, ['published ports'], 'TemplateEditor'), '8000')
    tree = await setValue(inputField(tree, ['published ports'], 'TemplateEditor'), '8000, 8000')
    tree = await click(findButtons(tree).byLabel('Save'))
    assert.ok(allText(tree).includes('Port 8000 is listed twice.'), allText(tree).slice(0, 400))
    assert.equal(callsMatching(fake, 'PATCH', /^\/templates\//).length, 0, 'nothing is sent')
    tree = await setValue(inputField(tree, ['published ports'], 'TemplateEditor'), '8000, 3000')
    tree = await click(findButtons(tree).byLabel('Save'))
    assert.deepEqual(bodyOf(callsMatching(fake, 'PATCH', /^\/templates\//)[0]).ports, [8000, 3000])
  })
  await withPlugin({ [`PATCH /templates/${PORT_TEMPLATE.id}`]: { status: 400, body: { error: { code: 'invalid', message: 'port 9999 is not allowed by this host.' } } } }, async () => {
    let tree = await mountComponent(C.TemplateEditor, { template: PORT_TEMPLATE, onClose() {} })

    tree = await setValue(inputField(tree, ['published ports'], 'TemplateEditor'), '9999')
    tree = await click(findButtons(tree).byLabel('Save'))
    assert.ok(allText(tree).includes('port 9999 is not allowed by this host.'), 'the backend refusal text')
  })
})

await check('the environment card lists published ports with Forward and Open in browser', () =>
  withPlugin({}, async (ctx, fake) => {
    const instance = withPorts(PLAIN, { '8000': '127.0.0.1:45557', novnc: '127.0.0.1:41234' })

    assert.deepEqual(T.publishedPorts(instance), [{ port: 8000, host: '127.0.0.1:45557' }], 'the screen port is not listed')

    let tree = await mountComponent(C.InstanceCard, { instance })

    assert.ok(allText(tree).includes('Published ports') && allText(tree).includes('Port 8000'), allText(tree).slice(0, 500))
    tree = await click(findButtons(tree).byLabel('Forward'))

    const posts = callsMatching(fake, 'POST', /^\/instances\/i-ab12cd34\/forward$/)

    assert.deepEqual(bodyOf(posts[0]), { remote_port: 8000 })
    assert.ok(allText(tree).includes('Port 8000 is on this computer at'), allText(tree))
    tree = await click(findButtons(tree).byLabel('Open in browser'))
    assert.equal(ctx.fake.osCalls.openExternal.at(-1), 'http://127.0.0.1:18080/')
  })
)

await check('no Published ports block for a stopped environment or one without published ports', () =>
  withPlugin({}, async () => {
    assert.ok(!allText(await mountComponent(C.InstanceCard, { instance: PLAIN })).includes('Published ports'))
    assert.ok(!allText(await mountComponent(C.InstanceCard, { instance: { ...STOPPED, ports: { '8000': '127.0.0.1:45557' } } })).includes('Published ports'))
  })
)

// =============================================================================
// chat cards for jobs, agent runs, saved copies and hosts
// =============================================================================
const cardEntry = (fake, name) => fake.find(sdk.TRANSCRIPT_DIRECTIVE_AREA, `${name}-card`)
const CARD_FIXTURES = {
  'cc-job': { valid: 'j-1a2b3c4d', invalid: ['j-1A2B3C4D', 'j-1a2b3c4', 'x-1a2b3c4d', '../jobs', 'j-1a2b3c4d/../x', ''], path: '/jobs/j-1a2b3c4d' },
  'cc-agent': { valid: 'a-1a2b3c4d', invalid: ['a-1A2B3C4D', 'a-1a2b3c4', 'j-1a2b3c4d', '../agents', ''], path: '/agents/a-1a2b3c4d' },
  'cc-copy': { valid: 'x-5e6f7a8b', invalid: ['x-5E6F7A8B', 'x-5e6f7a8', 'a-5e6f7a8b', '../exports', ''], path: '/exports/x-5e6f7a8b' },
  'cc-host': { valid: 'x1carbon', invalid: ['X1Carbon', 'x1/../carbon', '../hosts', 'x'.repeat(33), ''], path: '/hosts/x1carbon' }
}

await check('a card is registered for every directive (cc-env, cc-job, cc-agent, cc-copy, cc-host)', () =>
  withPlugin({}, async (ctx, fake) => {
    const names = fake.byArea(sdk.TRANSCRIPT_DIRECTIVE_AREA).map(r => r.data?.name).sort()

    assert.deepEqual(names, ['cc-agent', 'cc-copy', 'cc-env', 'cc-host', 'cc-job'])
    for (const name of Object.keys(CARD_FIXTURES)) {
      assert.equal(cardEntry(fake, name).id, `cloud-compute:${name}-card`)
      assert.equal(typeof cardEntry(fake, name).data.render, 'function')
    }
  })
)

await check('invalid ids render without a request; streaming renders a placeholder without a request', () =>
  withPlugin({}, async (ctx, fake) => {
    for (const [name, spec] of Object.entries(CARD_FIXTURES)) {
      for (const id of spec.invalid) {
        const element = cardEntry(fake, name).data.render({ attrs: { id }, sessionId: 's-1', streaming: false })

        await renderUntilStable(element)
      }

      await renderUntilStable(cardEntry(fake, name).data.render({ attrs: {}, sessionId: 's-1', streaming: false }))
      await renderUntilStable(cardEntry(fake, name).data.render({ attrs: { id: spec.valid }, sessionId: 's-1', streaming: true }))
    }

    assert.deepEqual(restLines(fake), [], 'no request for a bad id or while streaming')
  })
)

await check('a missing thing reads "no longer exists" with the reason', async () => {
  const gone = { status: 404, body: { error: { code: 'not_found', message: 'not found' } } }

  for (const [name, spec, expect] of [
    ['cc-job', CARD_FIXTURES['cc-job'], 'The job j-1a2b3c4d no longer exists. It was removed from the list.'],
    ['cc-agent', CARD_FIXTURES['cc-agent'], 'The cloud agent run a-1a2b3c4d no longer exists. Its record was removed.'],
    ['cc-copy', CARD_FIXTURES['cc-copy'], 'The saved copy x-5e6f7a8b no longer exists. It was deleted from this computer.'],
    ['cc-host', CARD_FIXTURES['cc-host'], 'The host x1carbon no longer exists. It was removed.']
  ]) {
    await withPlugin({ [`GET ${spec.path}`]: gone }, async (ctx, fake) => {
      const tree = await renderUntilStable(cardEntry(fake, name).data.render({ attrs: { id: spec.valid }, sessionId: null, streaming: false }))

      assert.deepEqual(restLines(fake), [`GET ${spec.path}`])
      assert.ok(allText(tree).includes(expect), `${name}: ${allText(tree)}`)
    })
  }
})

const renderCard = (fake, name, id) => renderUntilStable(cardEntry(fake, name).data.render({ attrs: { id: id ?? CARD_FIXTURES[name].valid }, sessionId: 's-1', streaming: false }))

await check('cc-job shows state colour, facts and Output; Cancel asks first', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await renderCard(fake, 'cc-job')

    assert.deepEqual(restLines(fake), ['GET /jobs/j-1a2b3c4d'])
    assert.ok(findAll(tree, n => n.name === 'JobChatCard').length >= 1)
    assert.ok(allText(tree).includes('train-small') && allText(tree).includes('RUNNING') && allText(tree).includes('Host slurm-x1'), allText(tree))
    assert.deepEqual(findAll(tree, n => n.name === 'StatusDot').map(n => n.props.tone), ['good'], 'running is green')
    assert.deepEqual(findButtons(tree).labels(), ['Output', 'Cancel'])
    tree = await click(findButtons(tree).byLabel('Output'))
    assert.ok(restLines(fake).includes('GET /jobs/j-1a2b3c4d/output?tail=200&stream=out'), restLines(fake).join())
    tree = await click(findButtons(tree).byLabel('Cancel'))
    assert.ok(!restLines(fake).includes('POST /jobs/j-1a2b3c4d/cancel'), 'Cancel waits for the confirmation')
    await openConfirm(tree).props.onConfirm()
    assert.ok(restLines(fake).includes('POST /jobs/j-1a2b3c4d/cancel'))
  })
)

await check('cc-job for a finished job offers Remove from list (confirmed, folder stays)', () =>
  withPlugin({ 'GET /jobs/j-1a2b3c4d': { body: jobsFixture.find(j => j.state === 'COMPLETED') } }, async (ctx, fake) => {
    let tree = await renderCard(fake, 'cc-job')

    assert.deepEqual(findButtons(tree).labels(), ['Output', 'Remove from list'])
    tree = await click(findButtons(tree).byLabel('Remove from list'))
    assert.ok(/on the cluster stays/.test(openConfirm(tree).props.description))
    await openConfirm(tree).props.onConfirm()
    assert.equal(fake.restCalls.filter(c => c.method === 'DELETE').length, 1)
  })
)

await check('cc-agent shows status, the reply and the model; Get results collects; Send a message opens the follow-up box', () =>
  withPlugin({ 'GET /agents/a-1a2b3c4d': { body: { ...readFixture('agent.json'), status: 'done', reply: 'Created NOTES.md and committed it.', turns: 2, model: 'qwen3-coder', endpoint_host: 'llm.example.net', commits_ahead: 1, ended_at: '2026-11-08T14:10:00Z' } } }, async (ctx, fake) => {
    let tree = await renderCard(fake, 'cc-agent')
    const text = allText(tree)

    assert.ok(findAll(tree, n => n.name === 'AgentChatCard').length >= 1)
    assert.ok(text.includes('done') && text.includes('Created NOTES.md and committed it.') && text.includes('Model qwen3-coder at llm.example.net') && text.includes('2 turns'), text)
    assert.deepEqual(findButtons(tree).labels(), ['Get results', 'Send a message\u2026', 'Open in Compute'])
    tree = await click(findButtons(tree).byLabel('Get results'))
    assert.ok(restLines(fake).includes('POST /agents/a-1a2b3c4d/collect'))
    assert.ok(allText(tree).includes('Results fetched'))
    tree = await click(findButtons(tree).byLabel('Send a message\u2026'))
    assert.ok(allText(tree).includes('Send a message to this agent'), 'the follow-up box is in the dialog')
  })
)

await check('cc-agent: a running run waits, a failed run shows its cause', async () => {
  await withPlugin({}, async (ctx, fake) => {
    const tree = await renderCard(fake, 'cc-agent')

    assert.ok(allText(tree).includes("Waiting for the agent's reply"), allText(tree))
    assert.deepEqual(findAll(tree, n => n.name === 'StatusDot').map(n => n.props.tone).slice(0, 1), ['good'])
    assert.ok(!findButtons(tree).labels().includes('Send a message\u2026'), 'no message while it works')
  })
  await withPlugin({ 'GET /agents/a-1a2b3c4d': { body: { ...readFixture('agent.json'), status: 'failed', error: 'The model answered HTTP 404.' } } }, async (ctx, fake) => {
    const tree = await renderCard(fake, 'cc-agent')

    assert.ok(allText(tree).includes('The run failed') && allText(tree).includes('The model answered HTTP 404.'))
    assert.deepEqual(findAll(tree, n => n.name === 'StatusDot').map(n => n.props.tone).slice(0, 1), ['bad'])
  })
})

await check('cc-copy shows size, files, path with Copy / Show in folder, and Restore into…', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await renderCard(fake, 'cc-copy')
    const text = allText(tree)

    assert.deepEqual(restLines(fake), ['GET /exports/x-5e6f7a8b'])
    assert.ok(text.includes('1.9 MB, 8 files from /work') && text.includes('python-dev'), text)
    assert.ok(text.includes('20261108T150100Z-before-delete.tar.gz'), 'the path')
    assert.deepEqual(findButtons(tree).labels().filter(l => l !== 'Copy'), ['Show in folder', 'Restore into\u2026'])
    tree = await click(findButtons(tree).byLabel('Restore into\u2026'))
    assert.ok(openDialog(tree, 'RestoreDialog'), 'the restore dialog opens')
  })
)

await check('cc-host shows status, resources and actions; Refresh status posts a probe', () =>
  withPlugin({}, async (ctx, fake) => {
    let tree = await renderCard(fake, 'cc-host')
    const first = readFixture('hosts.json')[0]

    assert.deepEqual(restLines(fake), ['GET /hosts/x1carbon'])
    assert.ok(findAll(tree, n => n.name === 'HostChatCard').length >= 1)
    assert.ok(allText(tree).includes(first.spec.name), allText(tree))
    assert.deepEqual(findButtons(tree).labels(), ['Refresh status', 'Open Hosts'])
    tree = await click(findButtons(tree).byLabel('Refresh status'))
    assert.ok(restLines(fake).includes('POST /hosts/x1carbon/probe'))
  })
)

await check('cc-host for a Slurm host adds Open shell and shows a failed work-directory check', () => {
  const slurm = readFixture('hosts_slurm.json')[0]

  slurm.state.capabilities.workdir_shared = false
  slurm.state.capabilities.workdir_detail = 'not visible from c1. Fix: use /data/cloud-compute'

  return withPlugin({ 'GET /hosts/slurm-x1': { body: slurm } }, async (ctx, fake) => {
    const tree = await renderUntilStable(cardEntry(fake, 'cc-host').data.render({ attrs: { id: 'slurm-x1' }, sessionId: null, streaming: false }))

    assert.ok(findButtons(tree).labels().includes('Open shell'))
    assert.ok(allText(tree).includes('Work directory check failed: not visible from c1'), allText(tree))
  })
})

await check('the cc-env card of a deleted environment says where its files were saved', () =>
  withPlugin({ 'GET /instances/i-ab12cd34': { body: { ...PLAIN, status: 'destroyed' } } }, async (ctx, fake) => {
    const entry = directive(fake)
    const tree = await renderUntilStable(entry.data.render({ attrs: { id: 'i-ab12cd34' }, sessionId: null, streaming: false }))
    const text = allText(tree)

    assert.ok(text.includes('This environment was deleted.'), text)
    assert.ok(text.includes('Its files were saved: 1.9 MB, 8 files from /work.'), text)
    assert.ok(text.includes('20261108T150100Z-before-delete.tar.gz'))
    assert.ok(restLines(fake).includes('GET /exports?instance_id=i-ab12cd34'), restLines(fake).join())
  })
)

// =============================================================================
// Remote-browser notes and box-drawing reply lines
// =============================================================================
await check('browserIsRemote and portOfAddress', () => {
  for (const name of ['', 'localhost', '127.0.0.1', '127.1.2.3', '::1', '[::1]', 'app.localhost']) assert.equal(T.browserIsRemote(name), false, name)
  for (const name of ['example.org', '192.168.1.20', 'box.lan', '10.0.0.5']) assert.equal(T.browserIsRemote(name), true, name)
  assert.equal(T.portOfAddress({ local_port: 39163 }), 39163)
  assert.equal(T.portOfAddress({ url: 'http://127.0.0.1:38035/' }), 38035)
  assert.equal(T.portOfAddress({}), null)
})

const REMOTE_SENTENCE = 'This address belongs to the computer that runs Hermes. From another computer, forward the port first:'
const TUNNEL_PORT = { ...readFixture('instance_forward.json'), local_port: 39163, url: 'http://127.0.0.1:39163/' }

await check('the Screen tab, the forward result and the tunnel list explain the address and give a copyable ssh command with the real port', async () => {
  await withPlugin({}, async () => {
    const tree = await mountComponent(C.ScreenViewer, { tunnel: { id: 't-1', url: 'http://127.0.0.1:39163/tok/vnc_lite.html', local_port: 39163 }, instance: DISPLAY })
    const text = allText(tree)

    assert.ok(text.includes('If the screen stays blank, use Open in browser.'))
    assert.ok(text.includes(REMOTE_SENTENCE), text.slice(0, 500))
    assert.ok(text.includes('ssh -N -L 39163:127.0.0.1:39163 <that computer>'))
    assert.ok(findAll(tree, n => n.name === 'CopyButton').some(n => n.props.text === 'ssh -N -L 39163:127.0.0.1:39163 <that computer>'), 'with a Copy button')
    assert.ok(!text.includes('Warning: this page is open on another computer'), 'neutral tone on a loopback page')
  })
  await withPlugin({ 'POST /instances/i-ab12cd34/forward': { body: TUNNEL_PORT } }, async () => {
    let tree = await mountComponent(C.ForwardDialog, { instance: PLAIN, onClose() {} })

    tree = await setValue(inputField(tree, ['port inside the environment'], 'ForwardDialog'), '8080')
    tree = await click(findButtons(tree).byLabel('Forward'))
    assert.ok(allText(tree).includes(REMOTE_SENTENCE) && allText(tree).includes('ssh -N -L 39163:127.0.0.1:39163'), allText(tree))
  })
  await withPlugin({}, async () => {
    const tree = await mountComponent(C.TunnelRow, { tunnel: TUNNEL_PORT })

    assert.ok(allText(tree).includes(REMOTE_SENTENCE) && allText(tree).includes('ssh -N -L 39163:127.0.0.1:39163'), allText(tree))
  })
})

await check('on a page whose address is not loopback the note is a warning', async () => {
  const saved = Object.getOwnPropertyDescriptor(globalThis, 'location')

  Object.defineProperty(globalThis, 'location', { value: { hostname: 'devbox.example.org' }, configurable: true })

  try {
    await withPlugin({}, async () => {
      const tree = await mountComponent(C.TunnelRow, { tunnel: TUNNEL_PORT })

      assert.ok(allText(tree).includes('Warning: this page is open on another computer, so this address will not work here.'), allText(tree))
      assert.ok(findAll(tree, n => n.props?.role === 'alert').length >= 1)
    })
  } finally {
    if (saved) Object.defineProperty(globalThis, 'location', saved)
    else delete globalThis.location
  }
})

await check('Terminal says where its window opens; the copy-the-command fallback repeats it', () =>
  withPlugin({ 'POST /instances/i-ab12cd34/terminal': { fixture: 'instance_terminal_unlaunched.json' } }, async (ctx, fake) => {
    T.resetTerminalGuard()

    let tree = await mountComponent(C.InstanceCard, { instance: PLAIN })
    const tip = findAll(tree, n => n.name === 'Tip' && String(n.props.label).startsWith('Opens a terminal window'))[0]

    assert.ok(tip.props.label.includes('The terminal window opens on the computer that runs Hermes. From another computer, copy the command and run it there.'))
    tree = await click(findButtons(tree).byLabel('Terminal'))
    assert.ok(notifyMessages().includes("Couldn't open a terminal. The command is on your clipboard."), JSON.stringify(notifyMessages()))
    assert.ok(notifyMessages().includes('The terminal window opens on the computer that runs Hermes. From another computer, copy the command and run it there.'))
  })
)

await check('a reply with box-drawing diff lines is shown whole in a monospace block', () =>
  withPlugin({}, async () => {
    const reply = '\u2502 NOTES.md\n\u2502 @@ -0,0 +1 @@\n\u2502 +tiny-calc is small\n\nCreated and committed NOTES.md.'
    const run = { ...runById('a-5e6f7a8b'), mode: 'hermes', status: 'done', reply }
    const tree = await mountComponent(C.AgentRunCard, { run, instances: [{ ...PLAIN, id: run.instance_id }] })
    const block = findAll(tree, n => typeof n.props?.className === 'string' && n.props.className.includes('whitespace-pre-wrap') && textOf(n).includes('@@ -0,0 +1 @@'))[0]

    assert.equal(T.replyHasBoxLines(reply), true)
    assert.equal(T.replyHasBoxLines('plain text'), false)
    assert.ok(block.props.className.includes('font-mono'), 'monospace')
    assert.equal(block.props.children, reply, 'nothing is stripped')
  })
)


// ---- page fixes: Stop, Run again, the approvals notice, the wizard, wording ----------
const restFailure = (status, code, message) => Object.assign(new Error(`${status}: ${message}`), { status, body: { error: { code, message } } })

await check('agentActions offers Get results and Stop for a stopped run whose sandbox was kept', () => {
  const kept = { status: 'stopped', instance_id: 'i-ab12cd34', commits_ahead: 2 }
  const live = [{ id: 'i-ab12cd34' }]

  assert.deepEqual(T.agentActions(kept, live), ['collect', 'stop'])
  assert.deepEqual(T.agentActions({ ...kept, commits_ahead: 0 }, live), ['stop'], 'nothing to fetch')
  assert.deepEqual(T.agentActions({ ...kept, collected_ref: 'refs/heads/cloud/a/x', collected_head_sha: 'abc', head_sha: 'abc' }, live), ['stop'], 'everything is fetched')
  assert.deepEqual(T.agentActions(kept, []), [], 'the sandbox is gone')
  assert.deepEqual(T.agentActions(kept), [], 'instance list not loaded yet')
})

await check('a stopped run with all commits fetched claims no new commits; one that could not be fetched says where they are', () =>
  withPlugin({}, async () => {
    const stopped = { ...runById('a-7f8e9d0c'), instance_id: '', collected_ref: 'refs/heads/cloud/a-7f8e9d0c/x', head_sha: 'b2c3d4e5f60718293a4b5c6d7e8f9012345678a1', collected_head_sha: 'b2c3d4e5f60718293a4b5c6d7e8f9012345678a1', commits_ahead: 2 }
    let text = allText(await mountComponent(C.AgentRunCard, { run: { ...stopped, stop_note: 'Fetched 1 new commit to branch cloud/a-7f8e9d0c/x before stopping.' }, instances: [] }))

    assert.ok(text.includes('Results fetched') && text.includes('Fetched 1 new commit to branch cloud/a-7f8e9d0c/x before stopping.'), text)
    assert.ok(!text.includes('new commits since'), text)
    assert.equal(T.agentResults(stopped, {}), 'fetched', 'no worktree is needed for fetched results')

    const lost = { ...stopped, collected_head_sha: 'a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1', stop_note: "The agent's newest commits could not be fetched (host offline) and the sandbox was deleted as asked. They are only in the saved copy of its files: C:/Users/me/exports/x.tar.gz." }

    text = allText(await mountComponent(C.AgentRunCard, { run: lost, instances: [] }))
    assert.ok(text.includes('C:/Users/me/exports/x.tar.gz'), text)
    assert.ok(!text.includes('new commits since'), 'no claim of fetchable commits when the sandbox is gone')
    assert.equal(findButtons(await mountComponent(C.AgentRunCard, { run: lost, instances: [] })).byLabel('Get new results'), undefined)
  })
)

await check('Stop names what it fetched in a toast', () =>
  withPlugin({ 'POST /agents/{id}/stop': async () => ({ ...readFixture('agent_stopped.json'), stop_note: 'Fetched 1 new commit to branch cloud/x/y before stopping.' }) }, async (ctx, fake) => {
    let tree = await mountComponent(C.AgentRunCard, { run: agentById('running') })

    tree = await click(findButtons(tree).byLabel('Stop'))
    assert.ok(allText(openConfirm(tree)).includes('fetched first'), allText(openConfirm(tree)))
    tree = await click(findButtons(openConfirm(tree)).byLabel('Stop'))
    assert.ok(toastTexts().some(t => t.includes('Stopped a-1a2b3c4d.') && t.includes('Fetched 1 new commit to branch cloud/x/y before stopping.')), JSON.stringify(toastTexts()))
  })
)

await check('when the commits cannot be fetched Stop deletes nothing and a second, separate confirmation can delete anyway', () => {
  const bodies = []
  const reason = "The agent's newest commits could not be fetched to this computer (host offline), so the sandbox was kept and nothing was deleted. Press Get results to try again. Deleting it anyway saves the files in /work to this computer first, and the commits are then only inside that saved copy, not on a branch."

  return withPlugin({
    'POST /agents/{id}/stop': async call => {
      const body = bodyOf(call)

      bodies.push(body)
      if (!body.delete_anyway) throw restFailure(409, 'not_fetched', reason)

      return { ...readFixture('agent_stopped.json'), stop_note: "The agent's newest commits could not be fetched (host offline) and the sandbox was deleted as asked. They are only in the saved copy of its files: C:/x/y.tar.gz." }
    }
  }, async () => {
    let tree = await mountComponent(C.AgentRunCard, { run: agentById('running') })

    tree = await click(findButtons(tree).byLabel('Stop'))
    tree = await click(findButtons(openConfirm(tree)).byLabel('Stop'))
    assert.deepEqual(bodies, [{ keep_branch: true }], 'the first request never deletes anyway')

    const second = openConfirm(tree)

    assert.ok(second, 'a second confirmation opens')
    assert.ok(allText(second).includes('nothing was deleted') && allText(second).includes('only inside that saved copy'), allText(second))
    assert.ok(findButtons(second).byLabel('Delete anyway'), 'clearly worded button')
    assert.equal(bodies.length, 1)

    tree = await click(findButtons(second).byLabel('Delete anyway'))
    assert.deepEqual(bodies[1], { keep_branch: true, delete_anyway: true })
    assert.ok(toastTexts().some(t => t.includes('C:/x/y.tar.gz')), JSON.stringify(toastTexts()))
  })
})

await check('Run again with Default model sends the default id and says which model will be used', () =>
  withPlugin({ 'GET /agent-models': { body: { ...MODELS_WITH_EXTRAS, models: [SAVED_MODEL, { ...SAVED_MODEL, id: 'm-2b2b2b2b', name: 'Wrong model', is_default: false }], default_id: SAVED_MODEL.id } } }, async (ctx, fake) => {
    const failed = { ...runById('a-9c0d1e2f'), model_id: 'm-2b2b2b2b' }
    let tree = await mountComponent(C.RerunDialog, { run: failed, hosts: readFixture('hosts.json'), onClose() {}, onFallback() {} })

    assert.ok(allText(tree).includes('Model for this run: Wrong model'), 'the old choice is preselected and named')
    tree = await click(findButtons(tree).find(b => b.label.includes('Default model')))
    assert.ok(allText(tree).includes('Model for this run: the default model, ' + SAVED_MODEL.name), allText(tree))
    tree = await click(findButtons(tree).byLabel('Run again'))

    const posts = callsMatching(fake, 'POST', /\/rerun$/)

    assert.equal(posts.length, 1)
    assert.deepEqual(bodyOf(posts[0]), { model_id: SAVED_MODEL.id }, 'the default is sent explicitly, not left out')
  })
)

await check('with no saved default the page sends the marker `default`', () =>
  withPlugin({ 'GET /agent-models': { body: { models: [{ ...SAVED_MODEL, id: 'm-2b2b2b2b', name: 'Wrong model', is_default: false }], detected: [], unavailable: [], default_id: null } } }, async (ctx, fake) => {
    const failed = { ...runById('a-9c0d1e2f'), model_id: 'm-2b2b2b2b' }
    let tree = await mountComponent(C.RerunDialog, { run: failed, hosts: readFixture('hosts.json'), onClose() {}, onFallback() {} })

    tree = await click(findButtons(tree).find(b => b.label.includes('Default model')))
    assert.ok(allText(tree).includes('Model for this run: the default model (the model set in Settings'), allText(tree))
    tree = await click(findButtons(tree).byLabel('Run again'))
    assert.deepEqual(bodyOf(callsMatching(fake, 'POST', /\/rerun$/)[0]), { model_id: 'default' })
  })
)

await check('the default saved choice is not listed twice', () =>
  withPlugin({ 'GET /agent-models': { body: MODELS_WITH_EXTRAS } }, async () => {
    let tree = await mountComponent(C.AgentsTab)

    tree = await click(findButtons(tree).byLabel('New agent run\u2026'))

    const rows = findButtons(openDialog(tree, 'NewAgentDialog')).labels()

    assert.equal(rows.filter(l => l.includes(SAVED_MODEL.model)).length, 1, JSON.stringify(rows))
  })
)

await check('the Compute page says when Hermes approvals are switched off, and only then', () => {
  const off = { ...readFixture('summary.json'), approvals: { mode: 'off', off: true } }

  return withPlugin({ 'GET /summary': { body: off } }, async () => {
    const notice = allText(await mountComponent(C.ApprovalsNotice))

    assert.equal(notice, 'Approvals are switched off in Hermes settings. Chat requests to start, change or delete things on your machines run without asking first. Turn approvals on in Settings to see a card before each one.')
    assert.ok(allText(await mountComponent(C.Page)).includes('Approvals are switched off in Hermes settings.'), 'shown on the page itself')
  }).then(() =>
    withPlugin({}, async () => {
      assert.equal(allText(await mountComponent(C.ApprovalsNotice)), '')
      assert.ok(!allText(await mountComponent(C.Page)).includes('Approvals are switched off'))
      assert.equal(T.approvalsOff({ approvals: { mode: 'manual', off: false } }), false)
      assert.equal(T.approvalsOff({}), false)
    })
  )
})

await check('the Delete dialog names the folder the backend recorded; an unknown folder is said in words', () =>
  withPlugin({}, async () => {
    let text = allText(await mountComponent(C.DeleteDialog, { instance: { ...PLAIN, workdir: '/data/work' }, onClose() {} }))

    assert.ok(text.includes('Save the files in /data/work to this computer first (recommended)'), text)
    assert.ok(!text.includes('files in /work to this'), 'not the template folder')

    text = allText(await mountComponent(C.DeleteDialog, { instance: { ...PLAIN, workdir: null }, onClose() {} }))
    assert.ok(text.includes('The work folder of python-dev is not recorded'), text)
    assert.ok(text.includes('Save its work folder (not recorded) to this computer first'), text)
  })
)

await check('the command-prefix hints say they can only be set on this page', () => {
  for (const key of ['exec_prefix', 'interactive_prefix']) {
    assert.ok(T.COPY.wizard_hints[key].endsWith('It can only be set here: the chat tool refuses it.'), key)
  }

  assert.ok(!JSON.stringify(T.COPY.wizard_hints).includes('`'), 'no literal backticks in a hint')
})

await check('folders that start with two backslashes are refused before anything is sent', () =>
  withPlugin({}, async (ctx, fake) => {
    assert.ok(T.specialPathError('\\\\server\\share\\x').includes('not accepted here'))
    assert.ok(T.specialPathError('"\\\\?\\C:\\Users"').includes('ordinary folder path on this computer'), 'quotes are ignored')
    assert.ok(!T.specialPathError('\\\\server\\share').includes('drive-letter'), 'no Windows-only example')
    assert.equal(T.specialPathError('C:\\Users\\me'), '')
    assert.equal(T.specialPathError('/home/me'), '')

    let tree = await mountComponent(C.SaveDialog, { instance: PLAIN, path: '/work', folder: '', onClose() {} })

    tree = await setValue(inputField(tree, ['save into folder (optional)'], 'SaveDialog'), '\\\\server\\share')
    tree = await click(findButtons(tree).find(b => b.label.startsWith('Save ')))
    assert.equal(callsMatching(fake, 'POST', /\/files\/fetch$/).length, 0, 'nothing was sent')
    assert.ok(allText(tree).includes('not accepted here'), allText(tree))

    let up = await mountComponent(C.UploadPanel, { instance: PLAIN, onClose() {}, onUploaded() {} })

    up = await setValue(inputField(up, ['path on this computer'], 'UploadPanel'), '\\\\?\\C:\\Users\\me\\secret')
    up = await click(findButtons(up).byLabel('Upload\u2026'))
    assert.ok(allText(up).includes('not accepted here') && !openConfirm(up), allText(up))
  })
)

await check('the wizard shows a failed work-directory check at once and the toast does not say all is well', () => {
  const failing = { ...readFixture('hosts_slurm.json')[0] }

  failing.state = { ...failing.state, capabilities: { ...(failing.state?.capabilities || {}), workdir_shared: false, workdir_detail: '~/cloud-compute is not visible from the compute nodes. Fix: Choose a Work directory on shared storage' } }

  return withPlugin({ 'POST /hosts': { body: failing } }, async () => {
    let tree = await mountComponent(C.AddHostWizard, { initial: { kind: 'slurm' }, onFinished() {} })

    tree = await click(findButtons(tree).byLabel('Next'))
    tree = await fillEndpoint(tree, 'login.example.org')
    tree = await click(findButtons(tree).byLabel('Next'))
    tree = await click(findButtons(tree).byLabel('Save'))

    const text = allText(tree)

    assert.ok(text.includes('Host saved, but its work directory check failed'), text)
    assert.ok(text.includes('Work directory check failed: ~/cloud-compute is not visible from the compute nodes.'), text)
    assert.ok(toastTexts().some(t => t.includes('its work directory check failed')), JSON.stringify(toastTexts()))
    assert.ok(!toastTexts().some(t => /^Added host [^,]*\.$/.test(t)), 'no plain success toast')
  })
})

await check('the Slurm wizard does not ask for environment limits', () =>
  withPlugin({ 'POST /hosts': { body: readFixture('hosts_slurm.json')[0] } }, async () => {
    let tree = await mountComponent(C.AddHostWizard, { initial: { kind: 'slurm' }, onFinished() {} })

    tree = await click(findButtons(tree).byLabel('Next'))
    tree = await fillEndpoint(tree, 'login.example.org')
    tree = await click(findButtons(tree).byLabel('Next'))

    const text = allText(tree)

    assert.ok(!text.includes('Max environments') && !text.includes('Max CPUs') && !text.includes('Max memory'), text)
    assert.ok(text.includes('Allow cloud agents on this host'))

    let ssh = await mountComponent(C.AddHostWizard, { initial: { kind: 'ssh' }, onFinished() {} })

    ssh = await click(findButtons(ssh).byLabel('Next'))
    ssh = await fillEndpoint(ssh, 'devbox')
    ssh = await click(findButtons(ssh).byLabel('Next'))
    assert.ok(allText(ssh).includes('Max environments') && allText(ssh).includes('Max CPUs'), 'an SSH host still has them')
  })
)

await check('a run that failed before the agent did any work offers no Get results, no turn count and no raw reply', () =>
  withPlugin({}, async () => {
    const failed = { ...runById('a-9c0d1e2f'), status: 'failed', commits_ahead: 0, turns: 1, error_kind: 'model_not_found', reply: 'Provider said: HTTP 404: {"error":"please check the model you provided"}', error: 'The model endpoint x answered 404: model m was not found there. It said: "please check the model you provided". Choose another model and start the run again.' }
    const tree = await mountComponent(C.AgentRunCard, { run: failed, instances: sandboxList(failed) })
    const text = allText(tree)

    assert.equal(findButtons(tree).byLabel('Get results'), undefined)
    assert.ok(!/\b1 turn\b/.test(text), text)
    assert.ok(!text.includes('{"error"') && !text.includes('Agent\'s reply'), text)
    assert.ok(text.includes('answered 404'), 'the cause sentence is shown')
    assert.equal(T.agentFailedBeforeWork(failed), true)
    assert.equal(T.agentFailedBeforeWork({ ...failed, commits_ahead: 1 }), false)

    const stopped = { ...failed, status: 'stopped', ended_at: new Date().toISOString() }
    const t3 = await mountComponent(C.AgentRunCard, { run: stopped, instances: [] })
    const stoppedText = allText(t3)

    assert.ok(!stoppedText.includes('Provider said') && !stoppedText.includes('{"error"') && !stoppedText.includes("Agent's reply"), 'after Stop the raw provider reply stays hidden: ' + stoppedText)
    assert.ok(stoppedText.includes('answered 404'), 'after Stop the cause sentence is still shown')

    const worked = { ...failed, commits_ahead: 2, turns: 3, error_kind: null }
    const t2 = await mountComponent(C.AgentRunCard, { run: worked, instances: sandboxList(worked) })

    assert.ok(findButtons(t2).byLabel('Get results'), 'a failed run with commits can still be fetched')
    assert.ok(allText(t2).includes('3 turns'))
  })
)

await check('a cancelled pending job does not claim it started', () => {
  const future = new Date(Date.now() + 28 * 60000).toISOString()

  assert.equal(T.jobStartFact({ state: 'CANCELLED', started_at: future, ended_at: new Date().toISOString() }), 'Cancelled before it started')
  assert.equal(T.jobStartFact({ state: 'CANCELLED', started_at: null, ended_at: new Date().toISOString() }), 'Cancelled before it started')
  assert.ok(T.jobStartFact({ state: 'PENDING', started_at: future }).startsWith('Expected to start in '), T.jobStartFact({ state: 'PENDING', started_at: future }))
  assert.ok(T.jobStartFact({ state: 'COMPLETED', started_at: new Date(Date.now() - 60000).toISOString() }).startsWith('Started '))
  assert.ok(!/Started in/.test(T.jobStartFact({ state: 'PENDING', started_at: future })))
})

await check('delete toasts say it once; the folder and the archive sizes are told apart', () => {
  assert.equal(T.deletedText('x', 'Deleted x without saving its files.'), 'Deleted x without saving its files.')
  assert.equal(T.deletedText('x', ''), 'Deleted x.')
  assert.equal(T.deletedText('x', 'Files kept elsewhere.'), 'Deleted x. Files kept elsewhere.')
})

await check('Overview order: running environments come before the others of the open group; stopped ones are their own group', () => {
  const mk = (id, status, at) => ({ id, status, last_activity_at: at })
  const out = T.splitInstances([
    mk('i-stop', 'stopped', '2026-11-10T10:00:00Z'),
    mk('i-err', 'error', '2026-11-10T09:00:00Z'),
    mk('i-new', 'creating', '2026-11-10T09:30:00Z'),
    mk('i-old', 'running', '2026-11-10T08:00:00Z'),
    mk('i-recent', 'running', '2026-11-10T11:00:00Z'),
    mk('i-exit', 'exited', '2026-11-10T07:00:00Z')
  ])

  assert.deepEqual(out.active.map(i => i.id), ['i-recent', 'i-old', 'i-new', 'i-err'])
  assert.deepEqual(out.stopped.map(i => i.id), ['i-stop', 'i-exit'])
})


await check('the saved path appears once in the delete notice, and the save result tells the folder size from the archive size', () =>
  withPlugin({}, async () => {
    const copy = { id: 'x-1', local_path: 'C:/exports/w/x.tar.gz', bytes: 4400, files: 3, source_path: '/work' }

    T.$deleted.set({ name: 'python-dev-1', instance_id: 'i-1', message: 'Deleted python-dev-1. The files in /work (3 files, 8.7 KB) were saved to C:/exports/w/x.tar.gz.', export: copy })

    let text = allText(await mountComponent(C.DeletedNotice))

    assert.equal(text.split('C:/exports/w/x.tar.gz').length - 1, 1, text)
    assert.ok(findAll(await mountComponent(C.DeletedNotice), n => n.name === 'CopyButton').some(n => n.props.text === copy.local_path), 'Copy path is still there')

    T.$deleted.set({ name: 'python-dev-1', instance_id: 'i-1', message: '', export: copy })
    text = allText(await mountComponent(C.DeletedNotice))
    assert.equal(text.split('C:/exports/w/x.tar.gz').length - 1, 1, 'without a message the path is shown by its own line')
    T.$deleted.set(null)

    text = allText(await mountComponent(C.SaveResult, { rec: { bytes: 4400, files: 3, local_path: 'C:/exports/w/x.tar.gz', source_path: '/work' }, workdir: '/work', folder: { bytes: 8900, files: 3 } }))
    assert.ok(text.includes('Saved 3 files from /work to this computer'), text)
    assert.ok(text.includes('The saved copy is 4.3 KB (compressed); the folder held 8.7 KB.'), text)
  })
)

await check('no global timers used (import, register, render, handlers)', () => {
  assert.deepEqual(
    timerCalls.map(c => c.kind),
    [],
    `plugin.js called a global timer (use ctx.setInterval / ctx.setTimeout):\n${timerCalls[0]?.stack}`
  )
})

finish()

function finish() {
  if (failures.length > 0) {
    console.error(`SMOKE FAILED: ${failures.length} of ${checks} checks failed\n`)
    failures.forEach((message, index) => console.error(`${index + 1}. ${message}\n`))
    process.exit(1)
  }

  console.log(`${checks} checks passed`)
  console.log('SMOKE OK')
  process.exit(0)
}
