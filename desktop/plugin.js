// Own Compute: desktop half.
//
// One plain ESM file, no build step. It talks to the Python half through
// ctx.rest (routes under /api/plugins/cloud-compute) and listens for the
// plugin.cloud-compute.* events that the backend broadcasts.
//
// Rules this file follows: imports only from the SDK, react and
// react/jsx-runtime; jsx()/jsxs() calls instead of JSX syntax; theme
// variables only for colour; timers only through ctx.setInterval and
// ctx.setTimeout; no direct DOM access while rendering.

import { useEffect, useMemo, useRef, useState } from 'react'
import { Fragment, jsx, jsxs } from 'react/jsx-runtime'
import {
  atom,
  Badge,
  Button,
  Checkbox,
  ConfirmDialog,
  CopyButton,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  EmptyState,
  ErrorState,
  host,
  Input,
  LogView,
  PALETTE_AREA,
  pluginSettingsHref,
  queryClient,
  relativeTime,
  ROUTES_AREA,
  SandboxedFrame,
  SegmentedControl,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  StatusDot,
  Streamdown,
  Switch,
  Tabs,
  TabsList,
  TabsTrigger,
  Textarea,
  Tip,
  TRANSCRIPT_DIRECTIVE_AREA,
  useQuery,
  useValue
} from '@hermes/plugin-sdk'

// ---------------------------------------------------------------------------
// Constants and embedded copy
// ---------------------------------------------------------------------------

const PLUGIN_ID = 'cloud-compute'
const ROUTE = '/cloud-compute'
const EVENT_PREFIX = 'plugin.cloud-compute.'
const EVENT_NAMES = [
  'hosts.changed',
  'host.status',
  'instances.changed',
  'jobs.changed',
  'job.finished',
  'tunnels.changed',
  'notify',
  'agents.changed',
  'instance.expiring'
]
const TOOL_PREFIXES = ['env_', 'slurm_', 'compute_', 'cloud_agent_']
const POLL_MS = 5000
const CHIP_POLL_MS = 30000
const HEALTH_POLL_MS = 15000
// Port 6080 of a display environment is the noVNC page; it needs its token path.
const DISPLAY_PORT = 6080
const AGENT_KEY_ENV = 'CC_AGENT_API_KEY'
const MISSING_BACKEND =
  'Turn on the Agent switch for Own Compute in Capabilities > Plugins, or run `hermes plugins enable cloud-compute`, then restart Desktop'
const MISSING_BACKEND_MORE = 'Already enabled? Restart the Hermes gateway, then press Retry. If it still fails, reinstall or update the plugin.'
const LOOPBACK_SCHEME = 'http:' + '//'
// A second click on the same Terminal button within this time is ignored.
const TERMINAL_COOLDOWN_MS = 3000

// Same object as COPY in core/docs_content.py. It is also the offline
// fallback when GET /host-types or GET /docs fail.
const COPY = {
  type_cards: {
    ssh: {
      kind: 'ssh',
      title: 'SSH host',
      one_liner: 'Run sandboxed containers on a Linux machine you can SSH into.',
      when_to_use: 'Your own laptop, workstation, homelab or VM.',
      learn_more_topic: 'ssh-host',
      mvp: true,
      capabilities: {
        containers: 'yes',
        batch_jobs: 'no',
        interactive_terminal: 'yes',
        display: 'partial',
        port_forward: 'yes',
        gpu: 'partial',
        agents: 'partial',
        costs_money: 'no'
      },
      capability_notes: {
        display: 'Needs the display image on the host; the Screen button offers to build it.',
        gpu: 'If the host has a GPU that Docker or Podman can use.',
        agents: "Off until allowed in the host's sandbox defaults."
      }
    },
    slurm: {
      kind: 'slurm',
      title: 'Slurm login node',
      one_liner: 'Submit batch jobs to an HPC cluster through its login node.',
      when_to_use: 'HPC or lab clusters, sweeps, scheduled work.',
      learn_more_topic: 'slurm',
      mvp: true,
      capabilities: {
        containers: 'no',
        batch_jobs: 'yes',
        interactive_terminal: 'partial',
        display: 'no',
        port_forward: 'no',
        gpu: 'partial',
        agents: 'no',
        costs_money: 'partial'
      },
      capability_notes: {
        interactive_terminal: 'A shell on the login node, or inside a running job.',
        gpu: 'If your cluster has GPUs.',
        costs_money: 'Only if your site bills for cluster time.'
      }
    },
    local: {
      kind: 'local',
      title: 'Local Docker/Podman',
      one_liner: 'Run containers on this computer.',
      when_to_use: 'Quick tests without another machine.',
      learn_more_topic: 'local',
      mvp: true,
      capabilities: {
        containers: 'yes',
        batch_jobs: 'no',
        interactive_terminal: 'yes',
        display: 'partial',
        port_forward: 'yes',
        gpu: 'partial',
        agents: 'partial',
        costs_money: 'no'
      },
      capability_notes: {
        display: 'Needs the display image on this computer; the Screen button offers to build it.',
        port_forward: 'Forwards the screen and any ports a template publishes.',
        gpu: 'If Docker or Podman GPU access works on this computer.',
        agents: "Off until allowed in the host's sandbox defaults."
      }
    },
    ec2: {
      kind: 'ec2',
      title: 'EC2',
      one_liner: 'Start an AWS machine on demand and run containers on it like an SSH host.',
      when_to_use: 'You need more CPU than you own, for a few hours, with a time limit.',
      learn_more_topic: 'ec2',
      mvp: true,
      capabilities: {
        containers: 'yes',
        batch_jobs: 'no',
        interactive_terminal: 'yes',
        display: 'partial',
        port_forward: 'yes',
        gpu: 'partial',
        agents: 'partial',
        costs_money: 'yes'
      },
      capability_notes: {
        display: 'Needs about 2 GB of memory; a 1 GB instance type is too small for it.',
        gpu: "Only with a GPU instance type that the AWS account's quota allows.",
        agents: "Off until allowed in the host's sandbox defaults.",
        costs_money: 'Billed by AWS while the machine runs, and a little for its disk while it is stopped.'
      }
    }
  },
  wizard_hints: {
    endpoint: 'Use an alias from your SSH config if you have one. Example: gpu-box.',
    login: 'Uses your SSH agent or the key file you choose. The key itself is never stored.',
    test_connection: 'Checks DNS, port 22, login, the container runtime or scheduler, display support and GPUs.',
    sandbox_defaults: 'Limits apply to environments the plugin starts on this host.',
    jump_host:
      'Optional. A gateway server to connect through (ssh -J), as [user@]host[:port]. Leave empty for a direct connection.',
    partition:
      'Optional. The Slurm partition for jobs that do not name one (sbatch -p). Leave empty to use the cluster default.',
    account:
      'Optional. The Slurm account added to every job (sbatch -A). Leave empty if the cluster does not ask for one.',
    qos:
      'Optional. The quality-of-service level added to every job (sbatch --qos). Leave empty unless the site asks for one.',
    workdir:
      'Where each job gets its own folder, holding its script and output. Jobs start there, but it is not a sandbox: they run as you and can reach anything your account can. The folder must be on storage the compute nodes also see (shared storage). Home folders are not shared on every cluster, and job output cannot be read from a folder that is not. Test connection checks this.',
    exec_prefix:
      'Advanced. Added before every command run on this host. Only for a scheduler inside a container, for example podman exec -i slurmctld. It can only be set here: the chat tool refuses it.',
    interactive_prefix:
      'Advanced. Used instead of the command prefix for terminal sessions that need a TTY, for example podman exec -it -w /data slurmctld. It can only be set here: the chat tool refuses it.',
    slurm_agents:
      'Cloud agents are off for Slurm hosts: an agent needs a long-running container, and most clusters do not allow long-running processes on login nodes.'
  },
  tooltips: {
    screen: 'Opens the sandbox desktop in a tab. Only for templates with a display.',
    screen_disabled: "This host type can't show a screen.",
    terminal:
      'Opens a terminal window on this computer, inside this environment. Windows Terminal or Command Prompt on Windows, Terminal on macOS, your usual terminal on Linux.',
    terminal_fallback: "Couldn't open a terminal. The command is on your clipboard.",
    forward_port: 'Makes a port inside the environment available at 127.0.0.1 on this computer.',
    send_preview:
      'Exactly what starts on the host: image, limits and the names of variables. Secret values are never shown.',
    idle_timeout: 'Stopped automatically after this many minutes without use.',
    command_prefix: 'Advanced. Only for schedulers inside a container.',
    refresh_status: "Re-reads this host's status, resources and capabilities now. Changes nothing on the host.",
    test_connection:
      'Runs the step-by-step connection checklist: DNS, network port, login, container runtime or scheduler, screen support and GPUs. Nothing is saved.',
    screen_not_enabled: 'Screen: not enabled in this template'
  },
  status_lights: {
    online: {
      tone: 'green',
      label: 'Online',
      meaning: 'Connected; runtime or scheduler working',
      action: ''
    },
    degraded: {
      tone: 'amber',
      label: 'Degraded',
      meaning: 'Connected, but the runtime/scheduler is missing or failing',
      action: 'open the host to see which check failed'
    },
    auth_required: {
      tone: 'amber',
      label: 'Needs login',
      meaning: 'The machine answers but SSH login needs a prompt',
      action: 'run ssh <alias> once in a terminal, then Retry'
    },
    offline: {
      tone: 'red',
      label: 'Offline',
      meaning: 'No answer on port 22',
      action: 'check the machine is on and on your network/VPN'
    },
    unknown: {
      tone: 'grey',
      label: 'Unknown',
      meaning: 'Not checked yet',
      action: 'Refresh status'
    }
  }
}

// Every route the UI may call. Paths use {id} for path parameters, except
// {topic} for documentation topics. Query strings are added at call time.
const API_CALLS = [
  { method: 'GET', path: '/health' },
  { method: 'GET', path: '/summary' },
  { method: 'GET', path: '/host-types' },
  { method: 'GET', path: '/docs' },
  { method: 'GET', path: '/docs/{topic}' },
  { method: 'GET', path: '/hosts' },
  { method: 'POST', path: '/hosts' },
  { method: 'POST', path: '/hosts/test' },
  { method: 'GET', path: '/hosts/{id}' },
  { method: 'PATCH', path: '/hosts/{id}' },
  { method: 'DELETE', path: '/hosts/{id}' },
  { method: 'POST', path: '/hosts/{id}/probe' },
  { method: 'POST', path: '/hosts/{id}/display-image' },
  { method: 'POST', path: '/hosts/{id}/terminal' },
  { method: 'GET', path: '/hosts/{id}/partitions' },
  { method: 'GET', path: '/templates' },
  { method: 'POST', path: '/templates' },
  { method: 'GET', path: '/templates/{id}' },
  { method: 'PATCH', path: '/templates/{id}' },
  { method: 'DELETE', path: '/templates/{id}' },
  { method: 'GET', path: '/templates/{id}/preview' },
  { method: 'GET', path: '/instances' },
  { method: 'GET', path: '/instances/{id}' },
  { method: 'POST', path: '/instances' },
  { method: 'POST', path: '/instances/{id}/{action}' },
  { method: 'POST', path: '/instances/stop-all' },
  { method: 'GET', path: '/instances/{id}/logs' },
  { method: 'GET', path: '/instances/{id}/files' },
  { method: 'GET', path: '/instances/{id}/files/size' },
  { method: 'POST', path: '/instances/{id}/files/fetch' },
  { method: 'POST', path: '/instances/{id}/files/put' },
  { method: 'GET', path: '/exports' },
  { method: 'GET', path: '/exports/{id}' },
  { method: 'POST', path: '/exports/{id}/restore' },
  { method: 'DELETE', path: '/exports/{id}' },
  { method: 'POST', path: '/instances/{id}/terminal' },
  { method: 'POST', path: '/instances/{id}/display' },
  { method: 'POST', path: '/instances/{id}/forward' },
  { method: 'GET', path: '/tunnels' },
  { method: 'DELETE', path: '/tunnels/{id}' },
  { method: 'GET', path: '/jobs' },
  { method: 'GET', path: '/jobs/{id}' },
  { method: 'POST', path: '/jobs' },
  { method: 'POST', path: '/jobs/clear-finished' },
  { method: 'DELETE', path: '/jobs/{id}' },
  { method: 'POST', path: '/jobs/{id}/cancel' },
  { method: 'GET', path: '/jobs/{id}/output' },
  { method: 'POST', path: '/jobs/{id}/terminal' },
  { method: 'GET', path: '/agents' },
  { method: 'GET', path: '/agents/{id}' },
  { method: 'POST', path: '/agents' },
  { method: 'POST', path: '/agents/{id}/collect' },
  { method: 'POST', path: '/agents/{id}/stop' },
  { method: 'POST', path: '/agents/{id}/message' },
  { method: 'POST', path: '/agents/{id}/rerun' },
  { method: 'DELETE', path: '/agents/{id}' },
  { method: 'GET', path: '/agent-models' },
  { method: 'POST', path: '/agent-models' }
]

const FALLBACK_TOPICS = [
  { topic: 'getting-started', title: 'Getting started' },
  { topic: 'ssh-host', title: 'SSH hosts' },
  { topic: 'slurm', title: 'Slurm login nodes' },
  { topic: 'local', title: 'Local Docker/Podman' },
  { topic: 'ec2', title: 'EC2' },
  { topic: 'templates', title: 'Environment templates' },
  { topic: 'keeping-data', title: 'Keeping your data' },
  { topic: 'display', title: 'Remote screen' },
  { topic: 'terminal', title: 'Terminals and ports' },
  { topic: 'chat', title: 'Using Own Compute from chat' },
  { topic: 'agents', title: 'Cloud agents' },
  { topic: 'security', title: 'Security and privacy' },
  { topic: 'troubleshooting', title: 'Troubleshooting' }
]

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'hosts', label: 'Hosts' },
  { id: 'templates', label: 'Templates' },
  { id: 'jobs', label: 'Jobs' },
  { id: 'agents', label: 'Agents' },
  { id: 'help', label: 'Help' }
]

// Shown instead of TABS until the first host exists.
const FIRST_RUN_TABS = [
  { id: 'start', label: 'Get started' },
  { id: 'help', label: 'Help' }
]

// Capability flags in the order the host-type catalog lists them.
const CAP_LABELS = [
  ['containers', 'Containers'],
  ['batch_jobs', 'Batch jobs'],
  ['interactive_terminal', 'Terminal'],
  ['display', 'Screen'],
  ['port_forward', 'Port forward'],
  ['gpu', 'GPU'],
  ['agents', 'Cloud agents'],
  ['costs_money', 'Costs money']
]

function capLabel(key) {
  const known = CAP_LABELS.find(([k]) => k === key)
  if (known) return known[1]
  const text = String(key).replace(/_/g, ' ')
  return text.charAt(0).toUpperCase() + text.slice(1)
}

const STEP_LABELS = {
  dns: 'DNS',
  tcp: 'Network port',
  auth: 'SSH login',
  runtime: 'Container runtime',
  scheduler: 'Scheduler',
  display: 'Screen support',
  workdir: 'Work directory',
  gpu: 'GPU',
  credentials: 'AWS key stored',
  identity: 'Key accepted',
  region: 'Region',
  image: 'Machine image',
  launch: 'Allowed to launch',
  address: 'Who may use SSH'
}

const TONE_MAP = { green: 'good', amber: 'warn', red: 'bad', grey: 'muted' }

// Shared class strings (theme variables only).
const CARD = 'flex flex-col gap-2 rounded-lg border border-(--ui-stroke-secondary) p-3'
const MUTED = 'text-(--ui-text-tertiary)'
const SECONDARY = 'text-(--ui-text-secondary)'
const SMALL = 'text-xs'

// ---------------------------------------------------------------------------
// Module state
// ---------------------------------------------------------------------------

let ctx = null

function setCtx(next) {
  ctx = next
  // A fresh registration starts without any remembered clicks. The New agent run
  // draft and the fetched-results notes are read again from ctx.storage.
  resetTerminalGuard()
  agentFetched = null
  draftLoaded = false
  $agentDraft.set(null)
}

// Palette entries and the wizard hand-off use this to tell the page which tab
// and dialog to open. The page clears it once a tab has consumed it.
const $intent = atom(null)

// The last delete that saved a copy first. The environment card is gone once the
// delete finishes, so the notice lives on the Overview tab and survives it.
const $deleted = atom(null)

function setIntent(tab, action, extra) {
  $intent.set(Object.assign({ tab, action: action || null, nonce: Date.now() + Math.random() }, extra || {}))
}

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

function h(type, props, ...kids) {
  const p = Object.assign({}, props)
  let key
  if (p.key !== undefined) {
    key = p.key
    delete p.key
  }
  const flat = kids.filter(k => k !== undefined)
  if (flat.length === 1) {
    p.children = flat[0]
    return jsx(type, p, key)
  }
  if (flat.length > 1) {
    p.children = flat
    return jsxs(type, p, key)
  }
  return jsx(type, p, key)
}

const cx = (...parts) => parts.filter(Boolean).join(' ')

function readAtom(a) {
  return a && typeof a.get === 'function' ? a.get() : null
}

function errInfo(err) {
  let raw = err && err.message ? String(err.message) : String(err || 'Unknown error')
  // Errors that crossed the desktop bridge carry a transport prefix and no
  // status field, so classify by the message text.
  raw = raw.replace(/^Error invoking remote method '[^']*':\s*/, '').replace(/^Error:\s*/, '')
  let status = Number(err && (err.statusCode || err.status)) || 0
  let body = raw
  const m = /^(\d{3}):\s*([\s\S]*)$/.exec(raw)
  if (m) {
    if (!status) status = Number(m[1])
    body = m[2]
  }
  let code = ''
  let message = body
  let parsed = err && err.body && typeof err.body === 'object' ? err.body : null
  if (!parsed) {
    try {
      parsed = JSON.parse(body)
    } catch (_) {
      parsed = null
    }
  }
  if (parsed && typeof parsed === 'object') {
    if (parsed.error && typeof parsed.error === 'object') {
      code = parsed.error.code || ''
      message = parsed.error.message || message
    } else if (typeof parsed.error === 'string') {
      message = parsed.error
    } else if (parsed.detail) {
      message = typeof parsed.detail === 'string' ? parsed.detail : JSON.stringify(parsed.detail)
    }
  }
  return { status, code, message: message || 'Request failed' }
}

function api(method, path, body) {
  const opts = { method }
  if (body !== undefined) opts.body = body
  return ctx.rest(path, opts)
}

function invalidateAll() {
  return queryClient.invalidateQueries({ queryKey: ['cloud-compute'] })
}

function toast(kind, message, extra) {
  try {
    const more = Object.assign({}, extra || {})
    if (typeof more.title === 'string') more.title = maskText(more.title)
    host.notify(Object.assign({ kind, message: maskText(message) }, more))
  } catch (_) {
    // A missing toast stack must never break an action.
  }
}

function queryKey(...parts) {
  return ['cloud-compute', readAtom(host.state.connectionId), readAtom(host.state.profile), ...parts]
}

// Scope-aware key builder for components: re-renders when the connection or
// profile changes, so cached data never leaks across them.
function useQueryKey() {
  const conn = useValue(host.state.connectionId)
  const profile = useValue(host.state.profile)
  return useMemo(() => (...parts) => ['cloud-compute', conn, profile, ...parts], [conn, profile])
}

function useAction() {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [code, setCode] = useState('')
  const [status, setStatus] = useState(0)
  const run = async fn => {
    setBusy(true)
    setError(null)
    setCode('')
    setStatus(0)
    try {
      return await fn()
    } catch (e) {
      const info = errInfo(e)
      setError(info.message)
      setCode(info.code || '')
      setStatus(info.status || 0)
      return undefined
    } finally {
      setBusy(false)
    }
  }
  return { busy, error, code, status, run, clear: () => setError(null) }
}

function qState(q) {
  if (!q) return 'loading'
  if (q.data !== undefined) return 'ready'
  if (q.isError || q.status === 'error') return 'error'
  return 'loading'
}

function statusLight(status) {
  return COPY.status_lights[status] || COPY.status_lights.unknown
}

// An EC2 host with no machine, or a stopped one, is in a normal state and not a failure: the
// page shows it neutrally, without the offline wording, the red error or the network hint.
const EC2_CALM = {
  none: ['No machine yet', 'Nothing is running and nothing is billed. Press Start machine to launch one'],
  stopped: ['Machine stopped', 'The disk is kept. Press Start machine to run it again'],
  launching: ['Machine starting', 'It is not ready yet'],
  starting: ['Machine starting', 'It is not ready yet'],
  stopping: ['Machine stopping', 'It will be stopped in a moment']
}
const EC2_NO_MACHINE_ADDRESS = 'no-machine.invalid'

// `{ status, light, calm }` of a host for its dot, label, error line and hint.
function hostLook(spec, state) {
  const status = state && state.status ? state.status : 'unknown'
  const isEc2 = !!spec && spec.kind === 'ec2' && status !== 'online'
  const machine = isEc2 ? (spec.ec2 && spec.ec2.machine_state) || 'none' : ''
  if (isEc2 && EC2_CALM[machine]) {
    const [label, meaning] = EC2_CALM[machine]
    return { status: 'unknown', light: Object.assign({}, statusLight('unknown'), { label, meaning, action: '' }), calm: true }
  }
  const light = statusLight(status)
  if (isEc2 && light.action) return { status, light: Object.assign({}, light, { action: 'open the machine panel on this card: Check AWS refreshes it, Stop machine switches it off' }), calm: false }
  return { status, light, calm: false }
}

// The address shown on a host card: nothing for the placeholder of an EC2 host without a machine.
function hostAddress(spec) {
  const address = (spec && spec.address) || ''
  return address === EC2_NO_MACHINE_ADDRESS ? '' : address
}

function ago(iso) {
  if (!iso) return 'never'
  const t = Date.parse(iso)
  if (!Number.isFinite(t)) return String(iso)
  try {
    return relativeTime(t)
  } catch (_) {
    return String(iso)
  }
}

function fmtMb(mb) {
  if (!Number.isFinite(mb)) return '?'
  return mb >= 1024 ? (mb / 1024).toFixed(1) + ' GB' : Math.round(mb) + ' MB'
}

function fmtGb(gb) {
  return Number.isFinite(gb) ? Math.round(gb * 10) / 10 + ' GB' : '?'
}

function fmtNum(n) {
  return Number.isFinite(n) ? String(Math.round(n * 10) / 10) : '?'
}

function slugify(s) {
  return String(s || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 32)
    .replace(/-+$/g, '')
}

function splitWords(s) {
  return String(s || '')
    .split(/\s+/)
    .filter(Boolean)
}

function nz(s) {
  const t = String(s == null ? '' : s).trim()
  return t ? t : null
}

function numOrNull(s) {
  const t = String(s == null ? '' : s).trim()
  if (!t) return null
  const n = Number(t)
  return Number.isFinite(n) ? n : null
}

// ---------------------------------------------------------------------------
// Pure model functions (also exported through __test__)
// ---------------------------------------------------------------------------

// A stopped or exited environment has no running container: a terminal, a screen
// and a forwarded port all need one, so those buttons wait until it is started.
function instanceAsleep(instance) {
  const s = instance && instance.status
  return s === 'stopped' || s === 'exited'
}

function instanceActions(instance) {
  const i = instance || {}
  const asleep = instanceAsleep(i)
  const out = []
  if (!asleep) out.push('terminal')
  if (!asleep && i.display_state !== 'unsupported' && i.display !== false) out.push('screen')
  if (!asleep) out.push('forward')
  out.push('logs')
  if (i.status !== 'creating' && i.status !== 'destroyed') out.push('files')
  if (i.status === 'running' && i.expires_at) out.push('extend')
  if (i.status === 'running') out.push('stop')
  if (i.status === 'stopped' || i.status === 'exited') out.push('start')
  out.push('destroy', 'chat')
  return out
}

// ---- environment order on the Overview ------------------------------------

const INSTANCE_RANK = { running: 0, creating: 1, error: 2 }

function instanceStamp(instance) {
  const t = Date.parse((instance && (instance.last_activity_at || instance.stopped_at || instance.created_at)) || '')
  return Number.isFinite(t) ? t : 0
}

// Running and starting environments first, then the ones that need a look; stopped,
// exited and deleted ones go to their own group. Each group puts the most recently
// used first.
function splitInstances(list) {
  const items = (Array.isArray(list) ? list : []).filter(Boolean)
  const parked = i => instanceAsleep(i) || i.status === 'destroyed'
  const recent = (a, b) => instanceStamp(b) - instanceStamp(a)
  const rank = i => (INSTANCE_RANK[i.status] === undefined ? 3 : INSTANCE_RANK[i.status])
  return {
    active: items.filter(i => !parked(i)).sort((a, b) => rank(a) - rank(b) || recent(a, b)),
    stopped: items.filter(parked).sort(recent)
  }
}

const JOB_CANCELLABLE = ['PENDING', 'RUNNING', 'COMPLETING']
const JOB_FAILED = ['FAILED', 'TIMEOUT', 'OUT_OF_MEMORY', 'NODE_FAIL']

// 'Started 5 min. ago' for a job that ran. A pending job carries the scheduler's guess of
// its start in the future; a cancelled one never started.
function jobStartFact(job) {
  const j = job || {}
  const t = Date.parse(j.started_at || '')
  const future = Number.isFinite(t) && t > Date.now()
  if (j.started_at && !future) return 'Started ' + ago(j.started_at)
  if (j.state === 'CANCELLED' || (j.ended_at && !Number.isFinite(t))) return 'Cancelled before it started'
  if (j.state === 'PENDING' && future) return 'Expected to start ' + ago(j.started_at)
  return j.submitted_at ? 'Submitted ' + ago(j.submitted_at) : null
}

// A job closed without a final state from Slurm (no accounting): UNKNOWN with an end time.
function jobUnconfirmed(job) {
  return !!job && job.state === 'UNKNOWN' && !!job.ended_at
}

// Badge text of a job. An unconfirmed end reads 'Ended (state unconfirmed)'.
function jobStateLabel(job) {
  if (jobUnconfirmed(job)) return 'Ended (state unconfirmed)'
  return (job && job.state) || 'UNKNOWN'
}

// 'output' is always available, 'cancel' while Slurm still owns the job, and
// 'terminal' only while it runs (the shell attaches to the running job).
function jobActions(job) {
  const state = job && job.state
  const out = ['output']
  if (JOB_CANCELLABLE.indexOf(state) >= 0) out.push('cancel')
  if (state === 'RUNNING') out.push('terminal')
  if (jobFinished(job)) out.push('delete')
  return out
}

// A finished job can be removed from the list (the backend refuses any other).
function jobFinished(job) {
  const state = job && job.state
  if (!state || JOB_CANCELLABLE.indexOf(state) >= 0) return false
  return state !== 'UNKNOWN' || !!job.ended_at
}

// Why a job's output is empty, in the backend's words (status: ok | empty | not_started | missing).
const OUTPUT_STATUS_LABELS = {
  empty: 'Printed nothing',
  not_started: 'Not started yet',
  missing: 'No output file here'
}

// What the job's folder holds is on the cluster; only this computer's record goes.
function jobRemoveText(job) {
  const dir = job && job.job_dir ? ' (' + job.job_dir + ')' : ''
  return 'This only removes the record of the job from the list on this computer. The job folder' + dir + ' on the cluster stays, with its output files.'
}

// Output text, or the reason there is none: the backend's note and status instead of a bare 'No output yet.'
function OutputBody({ data }) {
  const text = data && data.text
  const note = data && data.note
  const status = data && data.status
  if (text) {
    return h('div', { className: 'flex flex-col gap-1' }, h(LogView, { className: 'max-h-96' }, text), note ? h('div', { className: cx(SMALL, MUTED) }, note) : null)
  }
  const label = OUTPUT_STATUS_LABELS[status]
  return h(
    'div',
    { className: 'flex flex-col gap-1.5 rounded-md border border-(--ui-stroke-secondary) p-2.5' },
    label ? h('span', {}, h(Badge, { variant: status === 'missing' ? 'warn' : 'muted' }, label)) : null,
    h('div', { className: cx(SMALL, SECONDARY) }, note || 'No output yet.')
  )
}

const INSTANCE_ID_RE = /^i-[0-9a-f]{8}$/

function plural(n, one, many) {
  return n === 1 ? one : many
}

function chipModel(summary) {
  if (!summary || !summary.hosts) return { text: 'Compute', tone: 'idle' }
  const hosts = summary.hosts
  const total = hosts.total || 0
  if (total === 0) return { text: 'No hosts', tone: 'idle' }
  const running = summary.instances ? summary.instances.running || 0 : 0
  const jobs = summary.jobs ? summary.jobs.active || 0 : 0
  const text =
    (hosts.online || 0) + '/' + total + ' ' + plural(total, 'host', 'hosts') +
    ' \u00b7 ' + running + ' ' + plural(running, 'env', 'envs') +
    ' \u00b7 ' + jobs + ' ' + plural(jobs, 'job', 'jobs')
  const attention = Array.isArray(summary.attention) ? summary.attention : []
  let tone = 'ok'
  if ((hosts.offline || 0) > 0) tone = 'error'
  else if ((hosts.degraded || 0) > 0 || (hosts.auth_required || 0) > 0 || attention.length > 0) tone = 'warn'
  return { text, tone }
}

function shouldInvalidate(type, payload) {
  if (typeof type !== 'string') return false
  if (type.indexOf(EVENT_PREFIX) === 0) return true
  if (type === 'tool.complete') {
    const name = payload && (payload.name || payload.tool_name)
    return typeof name === 'string' && TOOL_PREFIXES.some(p => name.indexOf(p) === 0)
  }
  return false
}

function chipTone(tone) {
  return tone === 'ok' ? 'good' : tone === 'warn' ? 'warn' : tone === 'error' ? 'bad' : 'muted'
}

// ---- data safety: stop reasons, time limits, file sizes -------------------

const EXTEND_PRESETS = [
  { minutes: 30, label: '30 min' },
  { minutes: 60, label: '1 h' },
  { minutes: 240, label: '4 h' }
]
const EXTEND_MIN = 1
const EXTEND_MAX = 10080
// A running environment this close to its limit is flagged on its card.
const EXPIRY_SOON_MIN = 15
const DEFAULT_WORKDIR = '/work'

// Why a stopped environment is stopped. Every text says whether the files are kept.
function stopReasonText(instance) {
  const i = instance || {}
  if (i.status === 'exited') return 'Exited. Files are kept.'
  if (i.status !== 'stopped') return null
  switch (i.stop_reason) {
    case 'expired':
      return 'Stopped: time limit reached. Files are kept.'
    case 'idle':
      return 'Stopped after being idle. Files are kept.'
    case 'orphan':
      return 'Found on ' + (i.host_id || 'the host') + ' without a record. Files are kept.'
    case 'host-gone':
      return 'Stopped because its host was removed or cannot be reached. Files on that host were not touched.'
    default:
      return 'Stopped.'
  }
}

function fmtDuration(minutes) {
  const m = Math.max(0, Math.round(minutes))
  if (m < 60) return m + ' min'
  const hours = Math.floor(m / 60)
  const rest = m % 60
  if (hours < 48) return rest ? hours + ' h ' + rest + ' min' : hours + ' h'
  const days = Math.floor(hours / 24)
  const hrs = hours % 24
  return hrs ? days + ' d ' + hrs + ' h' : days + ' d'
}

function fmtEnd(iso) {
  const t = Date.parse(iso)
  if (!Number.isFinite(t)) return 'an unknown time'
  try {
    return new Date(t).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
  } catch (_) {
    return String(iso)
  }
}

function minutesLeft(instance, now) {
  const t = Date.parse(instance && instance.expires_at)
  if (!Number.isFinite(t)) return null
  return Math.ceil((t - (now == null ? Date.now() : now)) / 60000)
}

// The time-limit line of a running environment: text, and whether it is close.
function timeLimitInfo(instance, now) {
  const i = instance || {}
  if (i.status !== 'running') return null
  const left = minutesLeft(i, now)
  if (left === null) return { text: 'No time limit', soon: false, left: null }
  if (left <= 0) return { text: 'Time limit reached', soon: true, left: 0 }
  return {
    text: 'Time left ' + fmtDuration(left) + ' (ends ' + fmtEnd(i.expires_at) + ')',
    soon: left <= EXPIRY_SOON_MIN || !!i.expiry_warned_at,
    left
  }
}

// Starting after the time limit gives a fresh limit; the user is told before it happens.
function startNeedsNotice(instance, now) {
  const i = instance || {}
  if (i.status !== 'stopped' && i.status !== 'exited') return false
  if (i.stop_reason === 'expired') return true
  const t = Date.parse(i.expires_at)
  return Number.isFinite(t) && t <= (now == null ? Date.now() : now)
}

function freshLimitText(instance) {
  const m = instance && Number(instance.ttl_minutes)
  return Number.isFinite(m) && m > 0 ? 'a fresh time limit of ' + fmtDuration(m) : 'a fresh time limit'
}

// A plain-text message the backend attaches to a delete (for example that /work was
// empty). It is shown as it is; nothing is made up when it is absent.
function backendNote(rec) {
  if (!rec || typeof rec !== 'object') return ''
  for (const k of ['message', 'note']) {
    if (typeof rec[k] === 'string' && rec[k].trim()) return rec[k].trim()
  }
  return rec.status === 'destroyed' && typeof rec.error === 'string' ? rec.error.trim() : ''
}

function fmtBytes(n) {
  if (!Number.isFinite(n) || n < 0) return '?'
  if (n < 1024) return n + ' B'
  const units = ['KB', 'MB', 'GB', 'TB']
  let v = n
  let u = -1
  do {
    v /= 1024
    u += 1
  } while (v >= 1024 && u < units.length - 1)
  return (v >= 100 ? Math.round(v) : Math.round(v * 10) / 10) + ' ' + units[u]
}

// A path as typed or pasted: surrounding spaces and one pair of matching quotes
// ("Copy as path" in a file manager adds them) are not part of the path.
const QUOTE_PAIRS = { '"': '"', "'": "'", '\u201c': '\u201d', '\u2018': '\u2019' }

// The backend refuses folders that start with two backslashes (network shares, device paths).
// The page says so before sending, with the backend's reason (worded for any system: the
// backend's own message gives an example path of the system it runs on).
const SPECIAL_PATH_MESSAGE =
  'Paths that start with two backslashes or two slashes (a network share such as \\\\server\\share, or a device path such as \\\\?\\C:\\) are not accepted here, because they can point into a private folder (the SSH folder or the Hermes home) in a way that cannot be checked. Use an ordinary folder path on this computer instead.'

function specialPathError(value) {
  let t = String(value == null ? '' : value).trim()
  if (t.length >= 2 && QUOTE_PAIRS[t.charAt(0)] === t.charAt(t.length - 1)) t = t.slice(1, -1).trim()
  return t.indexOf('\\\\') === 0 ? SPECIAL_PATH_MESSAGE : ''
}

function cleanPath(value) {
  let t = String(value == null ? '' : value).trim()
  if (t.length >= 2 && QUOTE_PAIRS[t.charAt(0)] === t.charAt(t.length - 1)) t = t.slice(1, -1).trim()
  return t
}

// Size and age of a listed file or folder as one text with a separator: loose text
// nodes in a flex row run together ('3.2 MB28 days ago').
function fileFacts(entry) {
  const e = entry || {}
  return [e.type === 'dir' ? 'folder' : fmtBytes(e.size), e.mtime ? ago(e.mtime) : null].filter(Boolean).join(' \u00b7 ')
}

function joinPath(dir, name) {
  const base = String(dir || '/').replace(/\/+$/, '')
  return base + '/' + name
}

// Breadcrumb steps of an absolute path: '/work/a' -> /, work, a.
function pathCrumbs(path) {
  const parts = String(path || '/').split('/').filter(Boolean)
  const out = [{ label: '/', path: '/' }]
  let acc = ''
  parts.forEach(p => {
    acc += '/' + p
    out.push({ label: p, path: acc })
  })
  return out
}

const EXPORT_REASONS = {
  'before-delete': 'saved before delete',
  'auto-delete': 'saved before automatic delete',
  manual: 'saved from the Files dialog',
  fetch: 'downloaded'
}

// Plain wording for the errors the file routes return.
function fileErrorText(err) {
  const e = err || {}
  if (e.code === 'not_running') return 'Not running. Start the environment, then try again.'
  if (e.code === 'too_large') return 'Too large. ' + (e.message || '')
  if (e.status === 403) return 'Refused. ' + (e.message || 'That path is private and is never uploaded.')
  return e.message || 'Request failed'
}

// ---------------------------------------------------------------------------
// Actions that reach outside the page
// ---------------------------------------------------------------------------

// Screen, forwarded ports and terminals belong to the computer that runs Hermes. A browser on
// another computer (a Webapp opened remotely or through an ssh tunnel) cannot reach them
// without a tunnel of its own. The page cannot tell for sure (through a tunnel the address
// is still 127.0.0.1), so every place that shows such an address says so.
const REMOTE_ADDRESS_NOTE = 'This address belongs to the computer that runs Hermes. From another computer, forward the port first:'
const REMOTE_TERMINAL_NOTE = 'The terminal window opens on the computer that runs Hermes. From another computer, copy the command and run it there.'

function browserHostname() {
  const loc = typeof globalThis !== 'undefined' ? globalThis.location : null
  return loc && typeof loc.hostname === 'string' ? loc.hostname : ''
}

// True only when the page's own address is clearly not a loopback one.
function browserIsRemote(name) {
  const hostName = String(name == null ? browserHostname() : name).toLowerCase().replace(/^\[|\]$/g, '')
  if (!hostName) return false
  return !(hostName === 'localhost' || hostName === '::1' || /^127\./.test(hostName) || /\.localhost$/.test(hostName))
}

function portOfAddress(rec) {
  if (rec && Number.isInteger(rec.local_port)) return rec.local_port
  const m = rec && typeof rec.url === 'string' ? /^https?:\/\/[^/\s:]+:(\d+)/i.exec(rec.url) : null
  return m ? Number(m[1]) : null
}

function RemoteNote({ port, name }) {
  const remote = browserIsRemote(name)
  const command = Number.isInteger(port) ? 'ssh -N -L ' + port + ':127.0.0.1:' + port + ' <that computer>' : ''
  return h(
    'div',
    { className: cx('flex flex-col gap-1', SMALL, remote ? 'font-medium text-destructive' : MUTED), role: remote ? 'alert' : undefined },
    h('span', {}, (remote ? 'Warning: this page is open on another computer, so this address will not work here. ' : '') + REMOTE_ADDRESS_NOTE),
    command ? h('span', { className: 'flex flex-wrap items-center gap-2' }, h('code', { className: 'break-all' }, command), h(CopyButton, { text: command, label: 'Copy', size: 'sm' })) : null
  )
}

// Terminals opened a moment ago, by target. A repeat click would open a second window.
const terminalRecent = new Map()

function resetTerminalGuard() {
  terminalRecent.clear()
}

function terminalOpenedText(name) {
  return 'Terminal opened for ' + name + '. It can appear behind this window.'
}

async function openTerminal(target) {
  const mapKey = target.kind + ':' + target.id
  const now = Date.now()
  const last = terminalRecent.get(mapKey)
  if (last !== undefined && now - last < TERMINAL_COOLDOWN_MS) return null
  terminalRecent.set(mapKey, now)
  let res
  try {
    res = await api('POST', '/' + target.kind + 's/' + target.id + '/terminal')
  } catch (e) {
    terminalRecent.delete(mapKey)
    throw e
  }
  if (res && res.launched === false) {
    terminalRecent.delete(mapKey)
    await ctx.os.writeClipboard(res.copy_command)
    toast('warning', COPY.tooltips.terminal_fallback)
    toast('info', REMOTE_TERMINAL_NOTE)
  } else {
    toast('success', terminalOpenedText(target.name || target.id))
  }
  return res
}

// Keeps a Terminal button disabled for a moment after a click.
function useTerminalCooldown() {
  const [cooling, setCooling] = useState(false)
  const start = () => {
    setCooling(true)
    if (ctx && typeof ctx.setTimeout === 'function') ctx.setTimeout(() => setCooling(false), TERMINAL_COOLDOWN_MS)
    else setCooling(false)
  }
  return { cooling, start }
}

async function openScreen(instance) {
  const rec = await api('POST', '/instances/' + instance.id + '/display')
  const url = rec && rec.url
  if (!url) {
    throw new Error((rec && rec.error) || 'The screen is not ready yet. Try again in a moment.')
  }
  const title = 'Screen \u00b7 ' + (instance.name || instance.id)
  if (typeof host.openWorkspace === 'function') {
    const entry = screenEntry(instance.id)
    entry.ended = false
    const closer = host.openWorkspace(screenWorkspaceId(instance.id), {
      title,
      render: () => jsx(ScreenViewer, { key: rec.id || rec.url, tunnel: rec, instance })
    })
    entry.closer = typeof closer === 'function' ? closer : null
  } else {
    await ctx.os.openExternal(url)
  }
  return rec
}

// Screen tabs that are open, by instance id. When the environment stops or is
// destroyed its tunnel is closed under the tab, which would be a black frame.
// The tab is closed (host.closeWorkspace, when this desktop has it) or its
// content is replaced with an "ended" message.
const SCREEN_CHECK_MS = 15000
const openScreens = new Map()

function screenWorkspaceId(instanceId) {
  return 'cc-display-' + instanceId
}

function screenEntry(instanceId) {
  let entry = openScreens.get(instanceId)
  if (!entry) {
    entry = { ended: false, closer: null, listeners: new Set() }
    openScreens.set(instanceId, entry)
  }
  return entry
}

function closeScreenTab(instanceId) {
  const entry = openScreens.get(instanceId)
  try {
    if (typeof host.closeWorkspace === 'function') {
      host.closeWorkspace(screenWorkspaceId(instanceId))
      return true
    }
    if (entry && typeof entry.closer === 'function') {
      entry.closer()
      return true
    }
  } catch (_) {
    // Fall through: the "ended" message stays up.
  }
  return false
}

function canCloseScreenTab(instanceId) {
  const entry = openScreens.get(instanceId)
  return typeof host.closeWorkspace === 'function' || !!(entry && typeof entry.closer === 'function')
}

function endScreen(instanceId) {
  const entry = openScreens.get(instanceId)
  if (!entry || entry.ended) return
  entry.ended = true
  // Prefer closing the tab outright; otherwise every viewer shows the message.
  if (typeof host.closeWorkspace === 'function' && closeScreenTab(instanceId)) {
    openScreens.delete(instanceId)
    return
  }
  entry.listeners.forEach(fn => fn())
}

// Looks at every open Screen tab and ends the ones whose environment is no longer running.
async function checkScreens() {
  for (const id of [...openScreens.keys()]) {
    const entry = openScreens.get(id)
    if (!entry || entry.ended) continue
    let status = null
    try {
      const inst = await api('GET', '/instances/' + encodeURIComponent(id))
      status = inst && inst.status ? inst.status : null
    } catch (e) {
      // Gone (404) ends the screen; any other failure says nothing about the environment.
      status = errInfo(e).status === 404 ? 'destroyed' : null
    }
    if (status && status !== 'running') endScreen(id)
  }
}

// noVNC needs scripts and, for mouse capture, pointer lock. Nothing that would
// give the frame the app's origin is requested: it keeps an opaque origin.
const SCREEN_SANDBOX = 'allow-scripts allow-pointer-lock'

// Secret tokens must never be DISPLAYED. A screen URL carries one in its path
// (/<token>/vnc_lite.html?path=<token>/websockify) and a one-time password in
// its query. Only the visible text is masked: Open in browser and Copy keep
// using the full address.
const MASK = '\u2022\u2022\u2022\u2022'

function maskUrl(url) {
  const s = String(url == null ? '' : url)
  const m = /^([a-z][a-z0-9+.-]*:\/\/[^/?#\s]*)?([\s\S]*)$/i.exec(s)
  const head = (m && m[1]) || ''
  const rest = ((m && m[2]) || '')
    .replace(/([?&](?:password|token|pw)=)[^&#]*/gi, '$1' + MASK)
    .replace(/([?&]path=)[A-Za-z0-9_-]{16,}(?=[/&#]|$)/gi, '$1' + MASK)
    .replace(/(^|\/)[A-Za-z0-9_-]{16,}(?=[/?#]|$)/g, '$1' + MASK)
  return head + rest
}

// Masks every web address inside free text (errors, toasts).
function maskText(text) {
  return String(text == null ? '' : text).replace(/\b[a-z][a-z0-9+.-]*:\/\/[^\s"'<>)\]]+/gi, u => maskUrl(u))
}

function ScreenViewer(props) {
  const p = props || {}
  const tunnel = p.tunnel || {}
  const instance = p.instance || {}
  const label = instance.name || instance.id || 'environment'
  const title = 'Screen \u00b7 ' + label
  const [stopped, setStopped] = useState(false)
  const [ended, setEnded] = useState(() => !!(instance.id && screenEntry(instance.id).ended))
  const act = useAction()

  useEffect(() => {
    if (!instance.id) return undefined
    const entry = screenEntry(instance.id)
    const onEnd = () => setEnded(true)
    entry.listeners.add(onEnd)
    if (entry.ended) setEnded(true)
    // Events normally report this; the check covers a missed one.
    const timer = typeof ctx.setInterval === 'function' ? ctx.setInterval(() => checkScreens(), SCREEN_CHECK_MS) : null
    return () => {
      entry.listeners.delete(onEnd)
      // The tab was closed: stop watching it.
      if (!entry.listeners.size && openScreens.get(instance.id) === entry) openScreens.delete(instance.id)
      if (typeof timer === 'function') timer()
    }
  }, [instance.id])

  const stop = () =>
    act.run(async () => {
      await api('DELETE', '/tunnels/' + encodeURIComponent(tunnel.id))
      toast('success', 'Stopped the screen for ' + label + '.')
      invalidateAll()
      setStopped(true)
    })

  let content
  if (ended) {
    content = h(
      'div',
      { className: cx('flex flex-1 flex-col items-center justify-center gap-3 p-6 text-center', SMALL, MUTED) },
      h('div', {}, 'Screen ended (environment stopped)'),
      canCloseScreenTab(instance.id) ? h(Button, { size: 'xs', variant: 'outline', onClick: () => closeScreenTab(instance.id) }, 'Close') : h('div', {}, 'You can close this tab.')
    )
  } else if (stopped) {
    content = h(
      'div',
      { className: cx('flex flex-1 items-center justify-center p-6 text-center', SMALL, MUTED) },
      'The screen is stopped. Choose Screen on the environment to start it again.'
    )
  } else if (tunnel.url) {
    content = h(SandboxedFrame, { src: tunnel.url, title, sandbox: SCREEN_SANDBOX, className: 'min-h-0 w-full flex-1 border-0' })
  } else {
    content = h('div', { className: cx('flex flex-1 items-center justify-center p-6 text-center', SMALL, MUTED) }, 'The screen has no address yet. Try again in a moment.')
  }

  return h(
    'div',
    { className: 'flex h-full min-h-0 flex-col' },
    h(
      'div',
      { className: 'flex flex-wrap items-center justify-between gap-2 border-b border-(--ui-stroke-secondary) px-3 py-2' },
      h('span', { className: 'truncate text-sm font-semibold' }, title),
      h(
        'span',
        { className: 'flex gap-1.5' },
        h(Button, { size: 'xs', variant: 'outline', disabled: !tunnel.url || stopped || ended, onClick: () => ctx.os.openExternal(tunnel.url) }, 'Open in browser'),
        h(Button, { size: 'xs', variant: 'outline', disabled: act.busy || stopped || ended, onClick: stop }, 'Stop screen')
      )
    ),
    h('div', { className: cx('px-3 pt-1.5', SMALL, MUTED) }, 'If the screen stays blank, use Open in browser.'),
    h('div', { className: 'px-3 pt-1' }, h(RemoteNote, { port: portOfAddress(tunnel) })),
    act.error ? h('div', { className: 'px-3 pt-2' }, h(ErrorLine, { message: act.error })) : null,
    content
  )
}

async function sendToChat(instance) {
  const line =
    'Use the Own Compute environment "' + (instance.name || instance.id) +
    '" (id ' + instance.id + ') on host ' + instance.host_id + '.'
  let inserted = false
  if (host.composer && typeof host.composer.insertText === 'function') {
    try {
      inserted = await host.composer.insertText(readAtom(host.state.focusedSessionId) || null, line)
    } catch (_) {
      inserted = false
    }
  }
  if (inserted) {
    toast('success', 'Added to your message in the chat.')
    return 'inserted'
  }
  await ctx.os.writeClipboard(line)
  toast('info', 'No chat composer is open. The line is on your clipboard.')
  return 'clipboard'
}

// ---------------------------------------------------------------------------
// Generic UI pieces
// ---------------------------------------------------------------------------

function Loading(props) {
  return h('div', { className: cx('py-6 text-center', SMALL, MUTED) }, (props && props.label) || 'Loading\u2026')
}

function QueryError({ q, title }) {
  const info = errInfo(q && q.error)
  return h(
    ErrorState,
    { title: title || "Couldn't load this", description: maskText(info.message) },
    h(Button, { variant: 'outline', size: 'sm', onClick: () => invalidateAll() }, 'Retry')
  )
}

// Renders `children(data)` once the query has data; loading and error states
// are handled here so every tab shows the same ones.
function Async({ q, children, label }) {
  const state = qState(q)
  if (state === 'loading') return h(Loading, { label })
  if (state === 'error') return h(QueryError, { q })
  return children(q.data)
}

function Section({ title, actions, children }) {
  return h(
    'section',
    { className: 'flex flex-col gap-2' },
    h(
      'div',
      { className: 'flex items-center justify-between gap-2' },
      h('h2', { className: cx('text-xs font-semibold uppercase tracking-wide', SECONDARY) }, title),
      actions || null
    ),
    children
  )
}

function Modal({ open, onClose, title, description, children, footer, className }) {
  if (!open) return null
  return h(
    Dialog,
    {
      open: true,
      onOpenChange: next => {
        if (!next) onClose()
      }
    },
    h(
      DialogContent,
      { className: cx('max-w-2xl', className) },
      h(
        DialogHeader,
        {},
        h(DialogTitle, {}, title),
        description
          ? h(DialogDescription, {}, description)
          : h(DialogDescription, { className: 'sr-only' }, title)
      ),
      children,
      footer ? h(DialogFooter, {}, footer) : null
    )
  )
}

function Field({ label, hint, children, className }) {
  return h(
    'label',
    { className: cx('flex flex-col gap-1', SMALL, className) },
    h('span', { className: cx('font-medium', SECONDARY) }, label),
    children,
    hint ? h('span', { className: MUTED }, hint) : null
  )
}

function TextField({ label, hint, value, onChange, placeholder, type, disabled, className }) {
  return h(
    Field,
    { label, hint, className },
    h(Input, {
      value: value == null ? '' : value,
      type: type || 'text',
      placeholder,
      disabled,
      onChange: ev => onChange(ev.target.value)
    })
  )
}

function Pick({ value, onChange, options, placeholder, disabled }) {
  return h(
    Select,
    { value: value || undefined, onValueChange: onChange, disabled },
    h(SelectTrigger, { className: 'w-full' }, h(SelectValue, { placeholder: placeholder || 'Select\u2026' })),
    h(
      SelectContent,
      {},
      options.map(o => h(SelectItem, { key: o.value, value: o.value }, o.label))
    )
  )
}

function ToggleRow({ label, hint, checked, onChange, disabled }) {
  return h(
    'label',
    { className: cx('flex items-center justify-between gap-3', SMALL) },
    h(
      'span',
      { className: 'flex flex-col' },
      h('span', { className: cx('font-medium', SECONDARY) }, label),
      hint ? h('span', { className: MUTED }, hint) : null
    ),
    h(Switch, { checked: !!checked, onCheckedChange: onChange, disabled })
  )
}

function ErrorLine({ message }) {
  if (!message) return null
  // whitespace-pre-line keeps one error per line (two sbatch errors are not run together)
  return h('div', { className: cx('whitespace-pre-line text-destructive', SMALL), role: 'alert' }, maskText(message))
}

// CPU and RAM meters of a host card. Slurm hosts report what jobs have allocated
// (resources.basis === 'allocated'), so the labels say so. Memory used that is
// unknown (null) is 'RAM n/a' with the total only; it is never drawn as 0.
function hostMeters(res) {
  const allocated = !!res && res.basis === 'allocated'
  const memUnknown = !res || res.mem_used_mb === null || res.mem_used_mb === undefined || !Number.isFinite(res.mem_used_mb)
  const memTotal = !!res && Number.isFinite(res.mem_total_mb) && res.mem_total_mb > 0
  const ram = allocated ? 'RAM allocated' : 'RAM'
  return [
    { label: allocated ? 'CPU allocated' : 'CPU', used: res && res.cpu_used, total: res && res.cpu_total, fmt: fmtNum },
    memUnknown && memTotal
      ? { label: 'RAM n/a', used: null, total: res.mem_total_mb, fmt: fmtMb, totalOnly: true }
      : { label: ram, used: res && res.mem_used_mb, total: res && res.mem_total_mb, fmt: fmtMb }
  ]
}

function Meter({ label, used, total, fmt, totalOnly }) {
  const known = Number.isFinite(used) && Number.isFinite(total) && total > 0
  return h(
    'div',
    { className: 'flex min-w-36 flex-1 flex-col gap-0.5' },
    h(
      'div',
      { className: cx('flex justify-between text-[0.6875rem]', SECONDARY) },
      h('span', {}, label),
      h('span', { className: MUTED }, known ? fmt(used) + ' / ' + fmt(total) : totalOnly ? fmt(total) + ' total' : 'unknown')
    ),
    known
      ? h('progress', {
          value: Math.min(used, total),
          max: total,
          className: 'h-1.5 w-full',
          style: { accentColor: 'var(--ui-accent)' }
        })
      : null
  )
}

function LightDot({ status }) {
  const light = statusLight(status)
  return h(StatusDot, { tone: TONE_MAP[light.tone] || 'muted' })
}

// Chips for what a host type offers. A capability that depends on the machine
// ('partial') is an outline chip, and its plain-language condition is listed
// under the chips instead of the word 'partial'.
function lowerFirst(text) {
  const t = String(text || '')
  return t.charAt(0).toLowerCase() + t.slice(1)
}

function capNotes(type) {
  const t = type || {}
  if (t.capability_notes && typeof t.capability_notes === 'object') return t.capability_notes
  const card = COPY.type_cards[t.kind]
  return card && card.capability_notes ? card.capability_notes : {}
}

function CapChips({ capabilities, notes }) {
  const caps = capabilities || {}
  const hints = notes || {}
  const known = CAP_LABELS.map(([k]) => k)
  const keys = known.filter(k => k in caps).concat(Object.keys(caps).filter(k => !known.includes(k)))
  const shown = keys.filter(k => caps[k] && caps[k] !== 'no')
  const chips = shown.map(k => h(Badge, { key: k, variant: caps[k] === 'partial' ? 'outline' : 'muted' }, capLabel(k)))
  const conditions = shown.filter(k => typeof hints[k] === 'string' && hints[k])
  return h(
    'div',
    { className: 'flex flex-col gap-1' },
    h('div', { className: 'flex flex-wrap gap-1' }, chips),
    conditions.length
      ? h(
          'ul',
          { className: cx('flex flex-col gap-0.5', SMALL, MUTED) },
          conditions.map(k => h('li', { key: k }, capLabel(k) + ': ' + lowerFirst(hints[k])))
        )
      : null
  )
}

function hostChips(caps) {
  const c = caps || {}
  const chips = []
  if (c.runtime) chips.push(c.runtime + (c.rootless ? ' (rootless)' : ''))
  if (c.scheduler) chips.push('slurm' + (c.scheduler_version ? ' ' + c.scheduler_version : ''))
  if (c.apptainer_version) chips.push('apptainer')
  if (c.containers) chips.push('containers')
  if (c.batch_jobs) chips.push('batch jobs')
  if (c.interactive_terminal) chips.push('terminal')
  if (c.port_forward) chips.push('port forward')
  if (c.display === 'supported') chips.push('screen')
  else if (c.display === 'buildable') chips.push('screen (image to build)')
  if (Array.isArray(c.gpus) && c.gpus.length) chips.push('GPU x' + c.gpus.length)
  return chips
}

function TestChecklist({ result }) {
  if (!result) return null
  const light = statusLight(result.status)
  const steps = Array.isArray(result.steps) ? result.steps : []
  return h(
    'div',
    { className: 'flex flex-col gap-2' },
    h(
      'div',
      { className: cx('flex items-center gap-2 font-medium', SMALL) },
      h(LightDot, { status: result.status }),
      h('span', {}, result.ok ? 'Connection works' : 'Some checks failed'),
      h('span', { className: MUTED }, light.label)
    ),
    h(
      'ul',
      { className: 'flex flex-col gap-1.5' },
      steps.map(step => {
        const mark = step.ok === true ? '\u2713' : step.ok === false ? '\u2717' : '\u2013'
        const markText = step.ok === true ? 'passed' : step.ok === false ? 'failed' : 'skipped'
        return h(
          'li',
          { key: step.name, className: cx('flex gap-2', SMALL) },
          h(
            'span',
            { className: cx('w-4 shrink-0 text-center font-semibold', step.ok === false ? 'text-destructive' : SECONDARY), title: markText },
            mark
          ),
          h(
            'span',
            { className: 'flex min-w-0 flex-col' },
            h(
              'span',
              {},
              h('span', { className: 'font-medium' }, STEP_LABELS[step.name] || step.name),
              step.detail ? h('span', { className: SECONDARY }, ': ' + step.detail) : null,
              Number.isFinite(step.ms) ? h('span', { className: MUTED }, ' (' + Math.round(step.ms) + ' ms)') : null
            ),
            step.ok === false && step.hint ? h('span', { className: MUTED }, 'Fix: ' + step.hint) : null
          )
        )
      })
    )
  )
}

// ---------------------------------------------------------------------------
// Host type data (server first, embedded copy as fallback)
// ---------------------------------------------------------------------------

function pickTypes(data) {
  if (Array.isArray(data) && data.length) return data
  return Object.keys(COPY.type_cards).map(k => COPY.type_cards[k])
}

function useHostTypes() {
  const key = useQueryKey()
  const q = useQuery({
    queryKey: key('host-types'),
    queryFn: () => api('GET', '/host-types'),
    retry: false,
    staleTime: 60000
  })
  return { q, types: pickTypes(q && q.data) }
}

function typeTitle(kind) {
  const card = COPY.type_cards[kind]
  return card ? card.title : kind || 'Host'
}

function isComingLater(t) {
  return t.mvp === false
}

// EC2 hosts: the sentence every launch shows, and the wizard's field set.
const EC2_BILLING = 'AWS bills your account while the machine runs; a stopped machine still bills a small amount for its disk until it is terminated.'
const EC2_IMAGES = [
  { value: 'auto-al2023', label: 'Amazon Linux 2023 (latest, Docker)' },
  { value: 'auto-ubuntu', label: 'Ubuntu 24.04 LTS (latest, Podman)' }
]

function ec2Form(s) {
  const e = s || {}
  return {
    region: e.region || 'us-east-1',
    instance_type: e.instance_type || 't3.micro',
    image: e.image || 'auto-al2023',
    disk_gb: String(e.disk_gb || 10),
    max_lifetime_minutes: String(e.max_lifetime_minutes || 120),
    idle_stop_minutes: String(e.idle_stop_minutes == null ? 30 : e.idle_stop_minutes),
    auto_start: !!e.auto_start,
    ssh_cidr: e.ssh_cidr || '',
    subnet_id: e.subnet_id || '',
    security_group_id: e.security_group_id || '',
    access_key_secret: e.access_key_secret || 'AWS_ACCESS_KEY_ID',
    secret_key_secret: e.secret_key_secret || 'AWS_SECRET_ACCESS_KEY'
  }
}

function ec2Body(f) {
  const int = (v, d) => {
    const n = Math.round(Number(v))
    return Number.isFinite(n) ? n : d
  }
  return {
    region: f.region.trim(),
    instance_type: f.instance_type.trim(),
    image: f.image,
    disk_gb: int(f.disk_gb, 10),
    max_lifetime_minutes: int(f.max_lifetime_minutes, 120),
    idle_stop_minutes: int(f.idle_stop_minutes, 30),
    auto_start: !!f.auto_start,
    ssh_cidr: nz(f.ssh_cidr),
    subnet_id: nz(f.subnet_id),
    security_group_id: nz(f.security_group_id),
    access_key_secret: nz(f.access_key_secret) || 'AWS_ACCESS_KEY_ID',
    secret_key_secret: nz(f.secret_key_secret) || 'AWS_SECRET_ACCESS_KEY'
  }
}

// Step 2 of the wizard for an EC2 host: region, type, image, limits, who may use SSH.
function Ec2Fields({ form, patch }) {
  const e = form.ec2
  const set = next => patch({ ec2: Object.assign({}, e, next) })
  const free = useAction()
  const [types, setTypes] = useState(null)
  const listFree = () =>
    free.run(async () => {
      setTypes(await api('POST', '/ec2/free-tier-types', { ec2: ec2Body(e) }))
    })
  return h(
    'div',
    { className: 'flex flex-col gap-3' },
    h('div', { className: cx(SMALL, SECONDARY) }, 'Store the key of an IAM user made only for this plugin (never the root user) as the Hermes secrets AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY. Never paste a key here or in a chat; this host stores only the secret names. Other secret names can be set here, but the value of the named secret is sent to AWS, so choose only AWS keys; chat accepts only names that start with AWS_.'),
    h(
      'div',
      { className: 'grid gap-2 sm:grid-cols-2' },
      h(TextField, { label: 'Region', value: e.region, onChange: v => set({ region: v }), placeholder: 'us-east-1' }),
      h(TextField, { label: 'Instance type', value: e.instance_type, onChange: v => set({ instance_type: v }), placeholder: 't3.micro', hint: 'The smallest general-purpose type with about 1 GB is the default.' }),
      h(Field, { label: 'Image' }, h(Pick, { value: e.image, onChange: v => set({ image: v }), options: EC2_IMAGES })),
      h(TextField, { label: 'Root disk (GB)', value: e.disk_gb, onChange: v => set({ disk_gb: v }), type: 'number', hint: 'A whole number from 8 to 500. The default is 10.' }),
      h(TextField, { label: 'Max lifetime (minutes)', value: e.max_lifetime_minutes, onChange: v => set({ max_lifetime_minutes: v }), type: 'number', hint: 'A whole number from 15 to 1440 (24 hours). The machine has its own stop timer. At every boot it uses the lifetime last written to the machine: this value is written once this computer reaches the machine over SSH, so a lower value only counts from then on. Extend can add at most one more of these.' }),
      h(TextField, { label: 'Stop when idle (minutes, 0 = never)', value: e.idle_stop_minutes, onChange: v => set({ idle_stop_minutes: v }), type: 'number', hint: 'A whole number from 0 to 1440. Stops the machine after this long without a running environment.' }),
      h(TextField, { label: 'Who may use SSH (optional)', value: e.ssh_cidr, onChange: v => set({ ssh_cidr: v }), placeholder: 'empty: this computer only', hint: 'Empty: this computer\u2019s public address, looked up once at checkip.amazonaws.com.' }),
      h(TextField, { label: 'Subnet id (optional)', value: e.subnet_id, onChange: v => set({ subnet_id: v }), placeholder: 'default network' }),
      h(TextField, { label: 'Security group id (optional)', value: e.security_group_id, onChange: v => set({ security_group_id: v }), placeholder: 'one is created' })
    ),
    h(ToggleRow, { label: 'Start the machine when an environment needs it', hint: 'Off by default. The approval card says the machine will be started and billed.', checked: e.auto_start, onChange: v => set({ auto_start: v }) }),
    h('div', { className: cx(SMALL, MUTED) }, 'Environment templates run on this machine as containers, as on an SSH host. The Desktop (noVNC) image needs about 2 GB of memory; a 1 GB type is too small for it.'),
    h('div', { className: cx(SMALL, 'font-medium') }, EC2_BILLING),
    h(
      'div',
      { className: 'flex flex-col gap-1' },
      h('div', {}, h(Button, { variant: 'outline', size: 'xs', disabled: free.busy, onClick: listFree }, free.busy ? 'Asking AWS\u2026' : 'Show free-tier eligible types')),
      h(ErrorLine, { message: free.error }),
      types
        ? h(
            'div',
            { className: cx(SMALL, SECONDARY) },
            (types.types || []).length
              ? (types.types || []).map(t => t.instance_type + (t.memory_mib ? ' (' + Math.round(t.memory_mib / 1024 * 10) / 10 + ' GB)' : '')).join(', ') + '. ' + types.note
              : 'AWS reported no free-tier eligible types for this account and region.'
          )
        : null
    )
  )
}

// Within this long before its stop time a machine is on its way down: Extend is not offered.
const EC2_LAST_MINUTE_MS = 60000
const EC2_AT_LONGEST = 'Already at the longest run time for this host. Change Max lifetime on the host to allow more.'

// The machine of an EC2 host on its card: state, time left, and Start/Stop/Extend/Terminate.
function Ec2Machine({ spec }) {
  const e = spec.ec2 || {}
  const act = useAction()
  const [confirm, setConfirm] = useState(false)
  const [pending, setPending] = useState(null)
  const [found, setFound] = useState(null)
  const path = '/hosts/' + encodeURIComponent(spec.id || '') + '/machine/'
  // Start, Extend and Stop first show what they do and cost (the backend's text, the same as the
  // chat card) and wait for a click on the confirm button.
  const ask = (action, body, note) =>
    act.run(async () => {
      const query = action === 'extend' ? '?minutes=' + encodeURIComponent((body && body.minutes) || 60) : ''
      const res = await api('GET', path + action + '/confirm' + query)
      setPending({ action, body, note, message: (res && res.message) || '', blocked: !!(res && res.blocked) })
    })
  const run = (action, body, note) =>
    act.run(async () => {
      const res = await api('POST', path + action, body || {})
      setConfirm(false)
      setPending(null)
      if (res && res.untracked) setFound(res.untracked)
      if (note) toast('success', note)
      invalidateAll()
    })
  const untracked = (id, action) =>
    act.run(async () => {
      await api('POST', '/hosts/' + encodeURIComponent(spec.id || '') + '/untracked/' + encodeURIComponent(id) + '/' + action)
      toast('success', (action === 'stop' ? 'Stopping ' : 'Terminating ') + id + '.')
      setFound(null)
      invalidateAll()
    })
  // The instances found on AWS that no host tracks are loaded when the card shows (and again
  // when the machine changes), not only after Check AWS.
  useEffect(() => {
    let alive = true
    Promise.resolve()
      .then(() => api('GET', '/hosts/' + encodeURIComponent(spec.id || '') + '/machine'))
      .then(res => { if (alive && res && res.untracked) setFound(res.untracked) })
      .catch(() => {})
    return () => { alive = false }
  }, [spec.id, e.machine_state])
  const state = e.machine_state || 'none'
  const running = state === 'ready' || state === 'launching' || state === 'starting'
  const label = { none: 'No machine', launching: 'Launching', starting: 'Starting', ready: 'Running', stopping: 'Stopping', stopped: 'Stopped', terminating: 'Terminating', error: 'Not ready' }[state] || state
  const left = e.auto_stop_at && running ? ago(e.auto_stop_at) : ''
  // In the last minute the machine no longer takes connections: Extend would fail.
  const stopImminent = state === 'ready' && !!e.auto_stop_at && Date.parse(e.auto_stop_at) - Date.now() < EC2_LAST_MINUTE_MS
  // The backend says the largest Extend step that fits under its ceiling (0 = none); the rule is
  // not repeated here. A record without the field falls back to one hour, which the backend checks.
  const extendStep = e.extend_step_minutes === undefined || e.extend_step_minutes === null ? 60 : Number(e.extend_step_minutes) || 0
  const list = (found && found.untracked) || []
  return h(
    'div',
    { className: 'flex flex-col gap-1.5 rounded-md border border-(--ui-stroke-secondary) p-2' },
    h(
      'div',
      { className: cx('flex flex-wrap gap-x-4 gap-y-0.5', SMALL) },
      h('span', { className: 'font-medium' }, 'Machine: ' + label),
      e.instance_id ? h('span', { className: MUTED }, e.instance_id) : null,
      h('span', { className: MUTED }, (e.instance_type || '') + ' in ' + (e.region || '') + ', ' + (e.disk_gb || '') + ' GB disk'),
      left ? h('span', { className: MUTED }, 'Stops itself ' + left) : null
    ),
    e.machine_detail ? h('div', { className: cx(SMALL, state === 'error' ? 'text-destructive' : SECONDARY) }, e.machine_detail) : null,
    state === 'ready' && !stopImminent && extendStep <= 0 ? h('div', { className: cx(SMALL, SECONDARY) }, EC2_AT_LONGEST) : null,
    stopImminent ? h('div', { className: cx(SMALL, SECONDARY) }, 'The machine is about to stop at its time limit and may no longer accept connections. Start it again after it has stopped; the disk is kept.') : null,
    // The boot timer on the machine still has another lifetime than the setting (it is rewritten
    // as soon as Hermes reaches the machine): say what happens if Hermes closes before that.
    e.instance_id && e.unit_lifetime_minutes && Number(e.unit_lifetime_minutes) !== Number(e.max_lifetime_minutes)
      ? h('div', { className: cx(SMALL, SECONDARY) }, 'If Hermes closes before it reaches the machine, the machine stops after ' + fmtDuration(e.unit_lifetime_minutes) + ' instead.')
      : null,
    running ? h('div', { className: cx(SMALL, 'font-medium') }, EC2_BILLING) : null,
    h(ErrorLine, { message: act.error }),
    h(
      'div',
      { className: 'flex flex-wrap gap-1.5' },
      state === 'none' || state === 'stopped' || state === 'error'
        ? h(Button, { size: 'xs', disabled: act.busy, onClick: () => ask('start', null, 'Starting the machine of ' + (spec.name || spec.id) + '. ' + EC2_BILLING) }, 'Start machine')
        : null,
      e.instance_id && (state === 'ready' || state === 'launching' || state === 'starting' || state === 'error') ? h(Button, { size: 'xs', variant: 'outline', disabled: act.busy, onClick: () => ask('stop', null, 'Stopping the machine. Its disk and containers are kept (storage is still billed).') }, 'Stop machine') : null,
      state === 'ready' && !stopImminent && extendStep > 0 ? h(Button, { size: 'xs', variant: 'outline', disabled: act.busy, onClick: () => ask('extend', { minutes: extendStep }, 'The machine stops itself ' + fmtDuration(extendStep) + ' later.') }, 'Extend ' + fmtDuration(extendStep)) : null,
      (e.instance_id || e.launch_token) ? h(Button, { size: 'xs', variant: 'outline', disabled: act.busy, onClick: () => setConfirm(true) }, 'Terminate machine') : null,
      h(Button, { size: 'xs', variant: 'ghost', disabled: act.busy, onClick: () => run('reconcile') }, 'Check AWS')
    ),
    pending
      ? h(
          Modal,
          { open: true, onClose: () => setPending(null), title: { start: 'Start the machine?', stop: 'Stop the machine?', extend: 'Extend the machine?' }[pending.action] || 'Confirm', description: pending.blocked ? 'This cannot be done right now.' : 'Nothing happens until you confirm.' },
          h('div', { className: cx('flex flex-col gap-3', SMALL) }, h('p', { className: 'whitespace-pre-wrap break-words' }, pending.message), h(ErrorLine, { message: act.error }), h('div', { className: 'flex justify-end gap-2' }, h(Button, { variant: 'ghost', onClick: () => setPending(null) }, pending.blocked ? 'Close' : 'Cancel'), pending.blocked ? null : h(Button, { disabled: act.busy, onClick: () => run(pending.action, pending.body, pending.note) }, act.busy ? 'Working\u2026' : { start: 'Start machine', stop: 'Stop machine', extend: 'Extend 1 h' }[pending.action])))
        )
      : null,
    confirm
      ? h(
          'div',
          { className: cx('flex flex-col gap-1.5', SMALL) },
          h('span', {}, 'Terminating deletes the instance and its disk. Environments on it are deleted too: their files are saved to this computer first. If the machine is stopped, start it first, or delete without saving.'),
          h(
            'div',
            { className: 'flex flex-wrap gap-1.5' },
            h(Button, { size: 'xs', disabled: act.busy, onClick: () => run('terminate', { delete_environments: true, save: true }, 'Terminated the machine.') }, 'Save all and terminate'),
            h(Button, { size: 'xs', variant: 'outline', disabled: act.busy, onClick: () => run('terminate', { delete_environments: true, save: false }, 'Terminated the machine without saving.') }, 'Terminate without saving'),
            h(Button, { size: 'xs', variant: 'ghost', onClick: () => setConfirm(false) }, 'Cancel')
          )
        )
      : null,
    list.length
      ? h(
          'div',
          { className: cx('flex flex-col gap-1', SMALL) },
          h('span', { className: cx('font-medium', 'text-destructive') }, 'Found on AWS, not tracked: AWS bills the account for these until they are stopped or terminated. They carry the tag of this install and are never touched automatically.'),
          list.map(i =>
            h(
              'div',
              { key: i.instance_id, className: 'flex flex-wrap items-center gap-2' },
              h('span', {}, i.instance_id + ' (' + i.state + ', ' + i.instance_type + ')'),
              h(Button, { size: 'xs', variant: 'outline', disabled: act.busy, onClick: () => untracked(i.instance_id, 'stop') }, 'Stop'),
              h(Button, { size: 'xs', variant: 'outline', disabled: act.busy, onClick: () => untracked(i.instance_id, 'terminate') }, 'Terminate')
            )
          )
        )
      : null
  )
}

// ---------------------------------------------------------------------------
// Add-host wizard (also used to edit a host)
// ---------------------------------------------------------------------------

function formFromSpec(spec) {
  const s = spec || {}
  const slurm = s.slurm || {}
  const limits = s.limits || {}
  const manual = !!(s.user || (s.port && s.port !== 22))
  return {
    kind: s.kind || 'ssh',
    name: s.name || '',
    mode: manual ? 'manual' : 'alias',
    address: s.address || '',
    user: s.user || '',
    port: String(s.port || 22),
    identity_file: s.identity_file || '',
    jump_host: s.jump_host || '',
    runtime_pref: s.runtime_pref || 'auto',
    exec_prefix: (s.exec_prefix || []).join(' '),
    interactive_prefix: (s.interactive_prefix || []).join(' '),
    partition: slurm.default_partition || '',
    account: slurm.account || '',
    qos: slurm.qos || '',
    workdir: slurm.workdir || '',
    max_instances: String(limits.max_instances == null ? 4 : limits.max_instances),
    max_cpus: limits.max_cpus == null ? '' : String(limits.max_cpus),
    max_memory_gb: limits.max_memory_gb == null ? '' : String(limits.max_memory_gb),
    allow_agents: !!limits.allow_agents,
    ec2: ec2Form(s.ec2)
  }
}

function buildSpec(form, initial) {
  const kind = form.kind
  const address = kind === 'local' ? 'localhost' : kind === 'ec2' ? '' : form.address.trim()
  const name = form.name.trim() || (kind === 'ec2' ? 'AWS ' + form.ec2.region.trim() : address)
  const manual = form.mode === 'manual' && kind !== 'local'
  const port = manual ? Math.round(Number(form.port)) || 22 : 22
  const maxInstances = Math.round(Number(form.max_instances))
  const spec = {
    name,
    kind,
    address,
    user: manual ? nz(form.user) : null,
    port,
    identity_file: nz(form.identity_file),
    jump_host: nz(form.jump_host),
    runtime_pref: form.runtime_pref || 'auto',
    exec_prefix: splitWords(form.exec_prefix),
    interactive_prefix: splitWords(form.interactive_prefix),
    limits: {
      max_instances: Number.isFinite(maxInstances) && maxInstances > 0 ? maxInstances : 4,
      max_cpus: numOrNull(form.max_cpus),
      max_memory_gb: numOrNull(form.max_memory_gb),
      allow_agents: kind === 'slurm' ? false : !!form.allow_agents
    }
  }
  if (kind === 'slurm') {
    spec.slurm = Object.assign({}, (initial && initial.slurm) || {}, {
      default_partition: nz(form.partition),
      account: nz(form.account),
      qos: nz(form.qos),
      workdir: nz(form.workdir) || '~/cloud-compute'
    })
  }
  if (kind === 'ec2') {
    spec.ec2 = ec2Body(form.ec2)
    spec.user = null
    spec.identity_file = null
    spec.jump_host = null
    spec.exec_prefix = []
    spec.interactive_prefix = []
  }
  const id = initial && initial.id ? initial.id : slugify(name)
  if (id) spec.id = id
  return spec
}

function validateEndpoint(form) {
  if (form.kind === 'local') return ''
  if (form.kind === 'ec2') {
    if (!/^[a-z]{2}(-gov)?-[a-z]+-\d{1,2}$/.test(form.ec2.region.trim())) return 'Enter an AWS region name, for example eu-west-1.'
    if (!form.ec2.instance_type.trim()) return 'Enter an instance type such as t3.micro.'
    return ''
  }
  if (!form.address.trim()) {
    return form.mode === 'manual' ? 'Enter the host name or address.' : 'Enter an SSH alias.'
  }
  if (form.mode === 'manual') {
    const port = Number(form.port)
    if (!Number.isInteger(port) || port < 1 || port > 65535) return 'The port must be a number from 1 to 65535.'
  }
  return ''
}

function TypeCard({ type, selected, onSelect }) {
  const later = isComingLater(type)
  return h(
    'button',
    {
      type: 'button',
      disabled: later,
      onClick: () => onSelect(type.kind),
      'aria-pressed': selected,
      className: cx(
        'flex flex-col items-start gap-1.5 rounded-lg border p-3 text-left',
        selected ? 'border-(--ui-accent)' : 'border-(--ui-stroke-secondary)',
        later ? 'opacity-60' : ''
      )
    },
    h(
      'span',
      { className: 'flex w-full items-center justify-between gap-2' },
      h('span', { className: 'text-sm font-semibold' }, type.title),
      later ? h(Badge, { variant: 'outline' }, 'coming later') : null
    ),
    h('span', { className: cx(SMALL, SECONDARY) }, type.one_liner),
    type.when_to_use ? h('span', { className: cx(SMALL, MUTED) }, 'When to use: ' + type.when_to_use) : null,
    h(CapChips, { capabilities: type.capabilities, notes: capNotes(type) })
  )
}

// The failed work-directory check of a saved Slurm host ({spec, state}), as text, or ''.
function workdirFailure(saved) {
  const caps = (saved && saved.state && saved.state.capabilities) || {}
  return caps.workdir_shared === false && caps.workdir_detail ? String(caps.workdir_detail) : ''
}

function AddHostWizard(props) {
  const p = props || {}
  const initial = p.initial || null
  const editing = !!(initial && initial.id)
  const { types } = useHostTypes()
  const [step, setStep] = useState(editing ? 2 : 1)
  const [form, setForm] = useState(() => formFromSpec(initial))
  const [test, setTest] = useState(null)
  const [saved, setSaved] = useState(null)
  const [advanced, setAdvanced] = useState(() => !!(initial && ((initial.exec_prefix || []).length || (initial.interactive_prefix || []).length)))
  const [localError, setLocalError] = useState('')
  const saveAct = useAction()
  const testAct = useAction()

  const patch = (next, keepTest) => {
    setForm(f => Object.assign({}, f, next))
    if (!keepTest) setTest(null)
    setLocalError('')
  }

  const doTest = () => {
    const bad = validateEndpoint(form)
    if (bad) {
      setLocalError(bad)
      return
    }
    testAct.run(async () => {
      setTest(await api('POST', '/hosts/test', buildSpec(form, initial)))
    })
  }

  const goNext = () => {
    if (step === 2) {
      const bad = validateEndpoint(form)
      if (bad) {
        setLocalError(bad)
        return
      }
    }
    setStep(step + 1)
  }

  // The wizard ends once the host is saved; nothing is started on the user's behalf.
  const finish = () => {
    invalidateAll()
    if (p.onFinished) p.onFinished({ host: saved })
  }

  const doSave = () => {
    const bad = validateEndpoint(form)
    if (bad) {
      setLocalError(bad)
      setStep(2)
      return
    }
    saveAct.run(async () => {
      const spec = buildSpec(form, initial)
      let res
      if (editing) {
        const body = Object.assign({}, spec)
        delete body.id
        delete body.kind
        res = await api('PATCH', '/hosts/' + encodeURIComponent(initial.id), body)
        toast('success', 'Saved changes to ' + spec.name + '.')
        invalidateAll()
        if (p.onFinished) p.onFinished({ host: res, spawn: false })
        return
      }
      // The backend announces the new host (and /health flips to configured)
      // before this POST reply arrives. The parent must already be holding the
      // wizard up by then, or it swaps the page for the tabs and this wizard is
      // remounted empty. Tell it before the request leaves.
      if (p.onSaving) p.onSaving()
      try {
        res = await api('POST', '/hosts', spec)
      } catch (e) {
        if (p.onSaveFailed) p.onSaveFailed()
        throw e
      }
      const failed = workdirFailure(res)
      toast(failed ? 'warning' : 'success', failed ? 'Added host ' + spec.name + ', but its work directory check failed. Change the Work directory in Edit on the Hosts tab, then test again.' : 'Added host ' + spec.name + '.')
      setSaved(res)
      if (p.onSaved) p.onSaved(res)
      invalidateAll()
    })
  }

  const steps = ['Type', 'Endpoint and login', 'Sandbox defaults']
  const header = h(
    'ol',
    { className: cx('flex flex-wrap gap-3', SMALL) },
    steps.map((label, i) =>
      h(
        'li',
        { key: label, className: i + 1 === step ? 'font-semibold' : MUTED },
        'Step ' + (i + 1) + ': ' + label
      )
    )
  )

  // Done screen after a new host is saved.
  if (saved) {
    const state = saved.state || null
    const savedLook = hostLook(saved.spec, state)
    const light = savedLook.light
    // Slurm hosts run jobs, not environments.
    const slurmHost = ((saved.spec && saved.spec.kind) || form.kind) === 'slurm'
    const pointer = slurmHost
      ? 'Submit a batch job from the Jobs tab whenever you need one.'
      : 'Create an environment from the Templates tab whenever you need one.'
    const wdFailed = workdirFailure(saved)
    return h(
      'div',
      { className: 'flex flex-col gap-3' },
      h('div', { className: 'flex items-center gap-2 text-sm font-medium' }, h(LightDot, { status: savedLook.status }), (wdFailed ? 'Host saved, but its work directory check failed: ' : 'Host saved: ') + ((saved.spec && saved.spec.name) || form.name || form.address)),
      h('div', { className: cx(SMALL, SECONDARY) }, light.label + '. ' + light.meaning + '.'),
      wdFailed ? h('div', { className: cx('flex flex-col', SMALL) }, h('span', { className: 'text-destructive' }, 'Work directory check failed: ' + wdFailed), h('span', { className: MUTED }, 'Change the Work directory in Edit on the Hosts tab, then press Test connection again.')) : null,
      h('div', { className: cx(SMALL, MUTED) }, pointer),
      h('div', { className: 'flex flex-wrap gap-2' }, h(Button, { onClick: () => finish() }, 'Done'))
    )
  }

  let body = null
  if (step === 1) {
    body = h(
      'div',
      { className: 'grid gap-2 sm:grid-cols-2' },
      types.map(t => h(TypeCard, { key: t.kind, type: t, selected: form.kind === t.kind, onSelect: kind => patch({ kind }) }))
    )
  } else if (step === 2 && form.kind === 'ec2') {
    body = h(
      'div',
      { className: 'flex flex-col gap-3' },
      h(TextField, { label: 'Name (optional)', value: form.name, onChange: v => patch({ name: v }, true), placeholder: 'AWS ' + form.ec2.region }),
      h(Ec2Fields, { form, patch }),
      h(
        'div',
        { className: 'flex flex-col gap-2' },
        h('div', {}, h(Button, { variant: 'secondary', size: 'sm', disabled: testAct.busy, onClick: doTest }, testAct.busy ? 'Testing\u2026' : 'Test connection')),
        h('div', { className: cx(SMALL, MUTED) }, 'Checks the key, the region, the image and a launch with DryRun. Nothing is started.'),
        h(ErrorLine, { message: testAct.error }),
        h(TestChecklist, { result: test })
      )
    )
  } else if (step === 2) {
    const isLocal = form.kind === 'local'
    const isSlurm = form.kind === 'slurm'
    body = h(
      'div',
      { className: 'flex flex-col gap-3' },
      h(TextField, { label: 'Name (optional)', value: form.name, onChange: v => patch({ name: v }, true), placeholder: form.address || 'My machine' }),
      isLocal
        ? h('div', { className: cx(SMALL, SECONDARY) }, 'Nothing to enter. Own Compute uses Docker or Podman on this computer.')
        : h(
            Fragment,
            {},
            h(SegmentedControl, {
              value: form.mode,
              onChange: mode => patch({ mode }),
              options: [
                { id: 'alias', label: 'SSH alias' },
                { id: 'manual', label: 'Host and user' }
              ]
            }),
            form.mode === 'alias'
              ? h(TextField, { label: 'SSH alias', value: form.address, onChange: v => patch({ address: v }), placeholder: 'gpu-box', hint: COPY.wizard_hints.endpoint })
              : h(
                  'div',
                  { className: 'grid gap-2 sm:grid-cols-3' },
                  h(TextField, { label: 'Host', value: form.address, onChange: v => patch({ address: v }), placeholder: 'host.example.org', className: 'sm:col-span-2' }),
                  h(TextField, { label: 'Port', value: form.port, onChange: v => patch({ port: v }), type: 'number' }),
                  h(TextField, { label: 'User', value: form.user, onChange: v => patch({ user: v }), className: 'sm:col-span-3' }),
                  h('div', { className: cx(SMALL, MUTED, 'sm:col-span-3') }, COPY.wizard_hints.endpoint)
                ),
            h(TextField, { label: 'Identity file (optional)', value: form.identity_file, onChange: v => patch({ identity_file: v }), placeholder: 'Path to a key file', hint: COPY.wizard_hints.login }),
            h(TextField, { label: 'Jump host (optional)', value: form.jump_host, onChange: v => patch({ jump_host: v }), hint: COPY.wizard_hints.jump_host })
          ),
      isSlurm
        ? h(
            'div',
            { className: 'grid gap-2 sm:grid-cols-2' },
            h(TextField, { label: 'Default partition', value: form.partition, onChange: v => patch({ partition: v }), hint: COPY.wizard_hints.partition }),
            h(TextField, { label: 'Account', value: form.account, onChange: v => patch({ account: v }), hint: COPY.wizard_hints.account }),
            h(TextField, { label: 'QOS', value: form.qos, onChange: v => patch({ qos: v }), hint: COPY.wizard_hints.qos }),
            h(TextField, { label: 'Work directory', value: form.workdir, onChange: v => patch({ workdir: v }), placeholder: '~/cloud-compute', hint: COPY.wizard_hints.workdir })
          )
        : null,
      h(
        'div',
        { className: 'flex flex-col gap-2' },
        h(
          'div',
          {},
          h(Button, { variant: 'ghost', size: 'xs', onClick: () => setAdvanced(!advanced) }, advanced ? 'Hide advanced' : 'Advanced')
        ),
        advanced
          ? h(
              'div',
              { className: 'flex flex-col gap-2' },
              h(Tip, { label: COPY.tooltips.command_prefix }, h('div', {}, h(TextField, { label: 'Command prefix', value: form.exec_prefix, onChange: v => patch({ exec_prefix: v }), placeholder: 'podman exec -i slurmctld', hint: COPY.wizard_hints.exec_prefix }))),
              h(TextField, { label: 'Interactive command prefix', value: form.interactive_prefix, onChange: v => patch({ interactive_prefix: v }), placeholder: 'podman exec -it -w /data slurmctld', hint: COPY.wizard_hints.interactive_prefix }),
              isLocal || isSlurm
                ? null
                : h(
                    Field,
                    { label: 'Container runtime' },
                    h(Pick, {
                      value: form.runtime_pref,
                      onChange: v => patch({ runtime_pref: v }),
                      options: [
                        { value: 'auto', label: 'Detect automatically' },
                        { value: 'podman', label: 'Podman' },
                        { value: 'docker', label: 'Docker' }
                      ]
                    })
                  )
            )
          : null
      ),
      h(
        'div',
        { className: 'flex flex-col gap-2' },
        h(
          'div',
          { className: 'flex items-center gap-2' },
          h(Tip, { label: COPY.tooltips.test_connection }, h(Button, { variant: 'secondary', size: 'sm', disabled: testAct.busy, onClick: doTest }, testAct.busy ? 'Testing\u2026' : 'Test connection'))
        ),
        h('div', { className: cx(SMALL, MUTED) }, COPY.wizard_hints.test_connection),
        h(ErrorLine, { message: testAct.error }),
        h(TestChecklist, { result: test })
      )
    )
  } else {
    body = h(
      'div',
      { className: 'flex flex-col gap-3' },
      // A Slurm host runs jobs, not environments: its limits do not apply, so they are not asked.
      form.kind === 'slurm' ? null : h('div', { className: cx(SMALL, SECONDARY) }, COPY.wizard_hints.sandbox_defaults),
      form.kind === 'slurm'
        ? null
        : h(
            'div',
            { className: 'grid gap-2 sm:grid-cols-3' },
            h(TextField, { label: 'Max environments', value: form.max_instances, onChange: v => patch({ max_instances: v }, true), type: 'number' }),
            h(TextField, { label: 'Max CPUs (blank = no cap)', value: form.max_cpus, onChange: v => patch({ max_cpus: v }, true), type: 'number' }),
            h(TextField, { label: 'Max memory in GB (blank = no cap)', value: form.max_memory_gb, onChange: v => patch({ max_memory_gb: v }, true), type: 'number' })
          ),
      h(ToggleRow, {
        label: 'Allow cloud agents on this host',
        hint: form.kind === 'slurm' ? COPY.wizard_hints.slurm_agents : 'Off by default.',
        checked: form.kind === 'slurm' ? false : form.allow_agents,
        disabled: form.kind === 'slurm',
        onChange: v => patch({ allow_agents: v }, true)
      })
    )
  }

  return h(
    'div',
    { className: 'flex flex-col gap-3' },
    editing ? null : header,
    body,
    h(ErrorLine, { message: localError || saveAct.error }),
    h(
      'div',
      { className: 'flex flex-wrap justify-end gap-2' },
      !p.embedded && p.onCancel ? h(Button, { variant: 'ghost', onClick: p.onCancel }, 'Cancel') : null,
      step > (editing ? 2 : 1) ? h(Button, { variant: 'outline', onClick: () => setStep(step - 1) }, 'Back') : null,
      step < 3 ? h(Button, { onClick: goNext }, 'Next') : null,
      step === 3 ? h(Button, { disabled: saveAct.busy, onClick: doSave }, saveAct.busy ? 'Saving\u2026' : 'Save') : null
    )
  )
}

// ---------------------------------------------------------------------------
// Hosts tab
// ---------------------------------------------------------------------------

function HostCard(props) {
  const p = props || {}
  const item = p.item || p.entry || p.hostItem || {}
  const spec = item.spec || item
  const state = item.state || null
  const look = hostLook(spec, state)
  const status = look.status
  const light = look.light
  const caps = (state && state.capabilities) || {}
  const res = (state && state.resources) || null
  const act = useAction()
  const [testResult, setTestResult] = useState(null)
  const [editing, setEditing] = useState(false)
  const [removing, setRemoving] = useState(false)
  const [imageOut, setImageOut] = useState(null)

  const hostPath = '/hosts/' + encodeURIComponent(spec.id || '')
  const chips = hostChips(caps)
  const diskUsed =
    res && Number.isFinite(res.disk_total_gb) && Number.isFinite(res.disk_free_gb) ? res.disk_total_gb - res.disk_free_gb : null
  const gpus = (res && Array.isArray(res.gpus) && res.gpus.length ? res.gpus : null) || (Array.isArray(caps.gpus) ? caps.gpus : [])
  const partitions = Array.isArray(caps.partitions) ? caps.partitions : []

  const probe = () =>
    act.run(async () => {
      await api('POST', hostPath + '/probe')
      invalidateAll()
    })
  const runTest = () =>
    act.run(async () => {
      setTestResult(await api('POST', '/hosts/test', spec))
    })
  const buildImage = () =>
    act.run(async () => {
      setImageOut(await api('POST', hostPath + '/display-image'))
      invalidateAll()
    })
  const shell = useTerminalCooldown()
  const openShell = () => {
    if (shell.cooling) return undefined
    shell.start()
    return act.run(() => openTerminal({ kind: 'host', id: spec.id, name: spec.name || spec.id }))
  }
  const remove = async () => {
    let removed = null
    try {
      removed = await api('DELETE', hostPath)
    } catch (e) {
      throw new Error(errInfo(e).message)
    }
    // For an EC2 host the backend's notes say which key pair and security group it removed.
    toast('success', 'Removed host ' + (spec.name || spec.id) + '.' + (removed && Array.isArray(removed.notes) && removed.notes.length ? ' ' + removed.notes.join(' ') : ''))
    invalidateAll()
  }

  return h(
    'div',
    { className: CARD },
    h(
      'div',
      { className: 'flex flex-wrap items-center justify-between gap-2' },
      h(
        'div',
        { className: 'flex min-w-0 items-center gap-2' },
        h(LightDot, { status }),
        h('span', { className: 'truncate text-sm font-semibold' }, spec.name || spec.id),
        h(Badge, { variant: 'outline' }, typeTitle(spec.kind)),
        h(Tip, { label: light.meaning + (light.action ? '. ' + light.action : '') }, h('span', { className: cx(SMALL, SECONDARY) }, light.label))
      ),
      h('span', { className: cx(SMALL, MUTED) }, (spec.user ? spec.user + '@' : '') + hostAddress(spec))
    ),
    chips.length ? h('div', { className: 'flex flex-wrap gap-1' }, chips.map(c => h(Badge, { key: c, variant: 'muted' }, c))) : null,
    spec.kind === 'ec2' ? h(Ec2Machine, { spec }) : null,
    look.calm ? null : h(
      'div',
      { className: 'flex flex-wrap gap-3' },
      hostMeters(res).map(m => h(Meter, { key: m.label, label: m.label, used: m.used, total: m.total, fmt: m.fmt, totalOnly: m.totalOnly })),
      h(Meter, { label: 'Disk', used: diskUsed, total: res && res.disk_total_gb, fmt: fmtGb }),
      gpus.map((g, i) =>
        h(Meter, { key: 'gpu' + i, label: 'GPU ' + (g.index != null ? g.index : i), used: g.mem_used_mb, total: g.mem_total_mb, fmt: fmtMb })
      )
    ),
    partitions.length
      ? h(
          'div',
          { className: cx(SMALL, SECONDARY) },
          'Partitions: ' + partitions.map(pt => pt.name + ' (' + pt.cpus_idle + '/' + pt.cpus_total + ' CPUs idle)').join(', ')
        )
      : null,
    caps.display_reason && caps.display !== 'supported' ? h('div', { className: cx(SMALL, MUTED) }, 'Screen: ' + caps.display_reason) : null,
    caps.workdir_shared === false && caps.workdir_detail
      ? h('div', { className: cx('flex flex-col', SMALL) }, h('span', { className: 'text-destructive' }, 'Work directory check failed: ' + caps.workdir_detail), h('span', { className: MUTED }, 'Change the Work directory in Edit, then press Test connection again.'))
      : null,
    look.calm ? null : h(
      'div',
      { className: cx('flex flex-wrap gap-x-4 gap-y-0.5', SMALL, MUTED) },
      h('span', {}, 'Last seen ' + ago(state && state.last_seen)),
      state && Number.isFinite(state.latency_ms) ? h('span', {}, 'Latency ' + Math.round(state.latency_ms) + ' ms') : null
    ),
    state && state.last_error && !look.calm ? h('div', { className: cx('text-destructive', SMALL) }, 'Last error: ' + state.last_error) : null,
    light.action && status !== 'online' ? h('div', { className: cx(SMALL, MUTED) }, 'What to do: ' + light.action) : null,
    h(ErrorLine, { message: act.error }),
    h(
      'div',
      { className: 'flex flex-wrap gap-1.5' },
      h(Tip, { label: COPY.tooltips.refresh_status }, h(Button, { size: 'xs', variant: 'outline', disabled: act.busy, onClick: probe }, 'Refresh status')),
      h(Tip, { label: COPY.tooltips.test_connection }, h(Button, { size: 'xs', variant: 'outline', disabled: act.busy, onClick: runTest }, 'Test connection')),
      h(Button, { size: 'xs', variant: 'outline', onClick: () => setEditing(true) }, 'Edit'),
      spec.kind === 'slurm'
        ? h(Tip, { label: 'Opens a terminal window on this computer, with a shell on the login node.' }, h(Button, { size: 'xs', variant: 'outline', disabled: act.busy || shell.cooling, onClick: openShell }, 'Open shell'))
        : null,
      caps.display === 'buildable' ? h(Button, { size: 'xs', variant: 'outline', disabled: act.busy, onClick: buildImage }, 'Build display image') : null,
      h(Button, { size: 'xs', variant: 'ghost', onClick: () => setRemoving(true) }, 'Remove')
    ),
    h(
      Modal,
      { open: !!testResult, onClose: () => setTestResult(null), title: 'Connection test: ' + (spec.name || spec.id) },
      h(TestChecklist, { result: testResult })
    ),
    h(
      Modal,
      { open: !!imageOut, onClose: () => setImageOut(null), title: 'Display image build' },
      h('div', { className: cx(SMALL, SECONDARY) }, imageOut && imageOut.ok ? 'The display image is ready.' : 'The build did not finish.'),
      h(LogView, { className: 'max-h-80' }, (imageOut && imageOut.output_tail) || '')
    ),
    h(
      Modal,
      { open: editing, onClose: () => setEditing(false), title: 'Edit ' + (spec.name || spec.id) },
      editing
        ? h(AddHostWizard, {
            initial: spec,
            onCancel: () => setEditing(false),
            onFinished: () => setEditing(false)
          })
        : null
    ),
    removing
      ? h(ConfirmDialog, {
          open: true,
          destructive: true,
          title: 'Remove ' + (spec.name || spec.id) + '?',
          description: 'This only removes the host from Own Compute. It is refused while environments are still running on it.',
          confirmLabel: 'Remove',
          onClose: () => setRemoving(false),
          onConfirm: remove
        })
      : null
  )
}

// Hosts are listed in sections by type, in this order; other kinds follow.
const HOST_GROUPS = [
  { kind: 'ssh', title: 'SSH hosts' },
  { kind: 'slurm', title: 'Slurm login nodes' },
  { kind: 'local', title: 'Local Docker/Podman' },
  { kind: 'ec2', title: 'EC2' }
]

function groupHosts(list) {
  const items = Array.isArray(list) ? list : []
  const kindOf = item => (item && item.spec && item.spec.kind) || (item && item.kind) || 'other'
  const known = HOST_GROUPS.map(g => g.kind)
  const others = Array.from(new Set(items.map(kindOf).filter(k => !known.includes(k)))).sort()
  const groups = HOST_GROUPS.concat(others.map(k => ({ kind: k, title: capLabel(k) })))
  return groups
    .map(g => ({ kind: g.kind, title: g.title, items: items.filter(item => kindOf(item) === g.kind) }))
    .filter(g => g.items.length)
}

function HostGroup({ group, collapsed, onToggle, children, what }) {
  const noun = what || 'hosts'
  return h(
    'section',
    { className: 'flex flex-col gap-2', 'data-group': group.kind },
    h(
      'button',
      {
        type: 'button',
        onClick: onToggle,
        'aria-expanded': !collapsed,
        title: collapsed ? 'Show these ' + noun : 'Hide these ' + noun,
        className: cx('flex items-center gap-2 text-left text-xs font-semibold uppercase tracking-wide', SECONDARY)
      },
      h('span', { 'aria-hidden': true }, collapsed ? '\u25b8' : '\u25be'),
      h('span', {}, group.title + ' (' + group.items.length + ')')
    ),
    collapsed ? null : children
  )
}

function HostsTab() {
  const key = useQueryKey()
  const intent = useValue($intent)
  const [adding, setAdding] = useState(false)
  // Which sections are folded away, by host kind. Lives only as long as the tab.
  const [collapsed, setCollapsed] = useState({})
  const q = useQuery({
    queryKey: key('hosts'),
    queryFn: () => api('GET', '/hosts'),
    refetchInterval: POLL_MS,
    retry: 1
  })

  useEffect(() => {
    if (intent && intent.action === 'add-host') {
      setAdding(true)
      $intent.set(null)
    }
  }, [intent])

  return h(
    'div',
    { className: 'flex flex-col gap-3' },
    h(
      'div',
      { className: 'flex items-center justify-between gap-2' },
      h('div', { className: cx(SMALL, SECONDARY) }, 'Machines Own Compute can run work on.'),
      h(Button, { size: 'sm', onClick: () => setAdding(true) }, '+ Add host')
    ),
    h(Async, { q, label: 'Loading hosts\u2026' }, data => {
      const list = Array.isArray(data) ? data : []
      if (!list.length) {
        return h(EmptyState, { title: 'No hosts yet', description: 'Add a machine you can SSH into, a Slurm login node, or use this computer.' })
      }
      return h(
        'div',
        { className: 'flex flex-col gap-4' },
        groupHosts(list).map(group =>
          h(
            HostGroup,
            { key: group.kind, group, collapsed: !!collapsed[group.kind], onToggle: () => setCollapsed(c => Object.assign({}, c, { [group.kind]: !c[group.kind] })) },
            h(
              'div',
              { className: 'flex flex-col gap-3' },
              group.items.map(item => h(HostCard, { key: (item.spec && item.spec.id) || item.id, item }))
            )
          )
        )
      )
    }),
    h(
      Modal,
      { open: adding, onClose: () => setAdding(false), title: 'Add host', description: 'Connect a machine in three steps.' },
      adding
        ? h(AddHostWizard, {
            onCancel: () => setAdding(false),
            onFinished: () => setAdding(false)
          })
        : null
    )
  )
}

// ---------------------------------------------------------------------------
// Overview tab
// ---------------------------------------------------------------------------

function ForwardDialog({ instance, onClose }) {
  const [remote, setRemote] = useState('')
  const [local, setLocal] = useState('')
  // `rec` is the tunnel record. Its remote_port is the port published on the host,
  // which differs from the port the user typed, so the typed port is kept apart.
  const [rec, setRec] = useState(null)
  const [typed, setTyped] = useState(0)
  const act = useAction()

  const submit = () => {
    const port = Math.round(Number(remote))
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      return act.run(async () => {
        throw new Error('Enter a port from 1 to 65535.')
      })
    }
    return act.run(async () => {
      const body = { remote_port: port }
      const lp = Math.round(Number(local))
      if (local.trim() && Number.isInteger(lp) && lp > 0) body.local_port = lp
      const made = await api('POST', '/instances/' + instance.id + '/forward', body)
      setTyped(port)
      setRec(made)
      invalidateAll()
    })
  }

  const address = rec ? '127.0.0.1:' + rec.local_port : ''
  const url = rec ? rec.url || LOOPBACK_SCHEME + address : ''

  return h(
    Modal,
    { open: true, onClose, title: 'Forward port: ' + (instance.name || instance.id), description: COPY.tooltips.forward_port },
    rec
      ? h(
          'div',
          { className: 'flex flex-col gap-2' },
          h(
            'div',
            { className: cx('flex flex-wrap items-center gap-2', SMALL, SECONDARY) },
            'Container port ' + typed + ' \u2192',
            h('code', { className: 'text-sm' }, maskUrl(url)),
            h(CopyButton, { text: url, label: 'Copy', size: 'sm' })
          ),
          h(RemoteNote, { port: rec.local_port }),
          typed === DISPLAY_PORT
            ? h('div', { className: cx(SMALL, MUTED) }, "Port 6080 is the environment's screen. It needs its token path, so this address alone shows an error. Use Screen on the environment instead.")
            : null,
          h(
            'div',
            { className: 'flex gap-2' },
            h(Button, { size: 'sm', onClick: () => ctx.os.openExternal(url) }, 'Open in browser'),
            h(Button, { size: 'sm', variant: 'ghost', onClick: onClose }, 'Close')
          )
        )
      : h(
          'div',
          { className: 'flex flex-col gap-3' },
          h(TextField, { label: 'Port inside the environment', value: remote, onChange: setRemote, type: 'number', placeholder: '8888' }),
          h(TextField, { label: 'Port on this computer (optional)', value: local, onChange: setLocal, type: 'number', hint: 'Leave blank to pick a free port.' }),
          h(ErrorLine, { message: act.error }),
          h(
            'div',
            { className: 'flex justify-end gap-2' },
            h(Button, { variant: 'ghost', onClick: onClose }, 'Cancel'),
            h(Button, { disabled: act.busy, onClick: submit }, act.busy ? 'Forwarding\u2026' : 'Forward')
          )
        )
  )
}

function LogsDialog({ instance, onClose }) {
  const key = useQueryKey()
  const q = useQuery({
    queryKey: key('instance-logs', instance.id),
    queryFn: () => api('GET', '/instances/' + instance.id + '/logs?tail=200'),
    refetchInterval: POLL_MS,
    retry: 1
  })
  return h(
    Modal,
    {
      open: true,
      onClose,
      title: 'Logs: ' + (instance.name || instance.id),
      footer: h(Button, { variant: 'outline', size: 'sm', onClick: () => invalidateAll() }, 'Refresh')
    },
    h(Async, { q, label: 'Loading logs\u2026' }, data => h(OutputBody, { data }))
  )
}

// The folder a save copies. The environment's own record says which one (the folder it was
// created with); null means the backend does not know it, and a Delete is then refused. An
// answer without that field (an older backend) falls back to the template's folder.
function useWorkdir(instance) {
  const key = useQueryKey()
  const tplQ = useQuery({ queryKey: key('templates'), queryFn: () => api('GET', '/templates'), retry: 1 })
  if (instance && typeof instance.workdir === 'string' && instance.workdir.charAt(0) === '/') return instance.workdir
  if (instance && instance.workdir === null) return null
  const tpl = (Array.isArray(tplQ.data) ? tplQ.data : []).find(t => t.id === (instance && instance.template_id))
  return (tpl && typeof tpl.workdir === 'string' && tpl.workdir.charAt(0) === '/' && tpl.workdir) || DEFAULT_WORKDIR
}

// ---- Extend -----------------------------------------------------------------

function ExtendDialog({ instance, onClose }) {
  const name = instance.name || instance.id
  const [minutes, setMinutes] = useState('60')
  const act = useAction()
  const value = Number(String(minutes).trim())
  const valid = String(minutes).trim() !== '' && Number.isInteger(value) && value >= EXTEND_MIN && value <= EXTEND_MAX
  const submit = () => {
    if (!valid) {
      return act.run(async () => {
        throw new Error('Enter a whole number of minutes from ' + EXTEND_MIN + ' to ' + EXTEND_MAX + '.')
      })
    }
    return act.run(async () => {
      const rec = await api('POST', '/instances/' + instance.id + '/extend', { minutes: value })
      toast('success', name + ' now ends ' + fmtEnd(rec && rec.expires_at) + '.')
      invalidateAll()
      onClose()
    })
  }
  return h(
    Modal,
    {
      open: true,
      onClose,
      title: 'Extend ' + name,
      description: 'Adds time to the limit. When the limit is reached the environment is stopped, not deleted, and its files are kept.'
    },
    h(
      'div',
      { className: 'flex flex-col gap-3' },
      h(
        'div',
        { className: 'flex flex-wrap gap-2' },
        EXTEND_PRESETS.map(pr =>
          h(Button, { key: pr.minutes, size: 'sm', variant: String(pr.minutes) === String(minutes).trim() ? 'default' : 'outline', onClick: () => setMinutes(String(pr.minutes)) }, pr.label)
        )
      ),
      h(TextField, { label: 'Minutes to add (1-10080)', value: minutes, onChange: setMinutes, type: 'number' }),
      h(ErrorLine, { message: act.error }),
      h(
        'div',
        { className: 'flex justify-end gap-2' },
        h(Button, { variant: 'ghost', onClick: onClose }, 'Cancel'),
        h(Button, { disabled: act.busy, onClick: submit }, act.busy ? 'Extending\u2026' : 'Extend')
      )
    )
  )
}

// ---- Delete -------------------------------------------------------------------

// The backend's sentence already starts with 'Deleted <name>.'; it is not said twice.
function deletedText(name, note) {
  const text = String(note || '').trim()
  if (!text) return 'Deleted ' + name + '.'
  return /^Deleted /.test(text) ? text : 'Deleted ' + name + '. ' + text
}

// Saves first by default. A failed save deletes nothing (409 not_saved); only then is
// 'Delete without saving' offered, and it asks once more.
function DeleteDialog({ instance, onClose }) {
  const name = instance.name || instance.id
  const [save, setSave] = useState(true)
  const [step, setStep] = useState('ask')
  const act = useAction()
  const recorded = useWorkdir(instance)
  const known = recorded !== null
  // The folder a Delete saves; named as the backend will save it. Unknown: said in words.
  const workdir = known ? recorded : 'its work folder'

  // `note` is the backend's own message about the save, shown as it is; `saved` is the
  // saved copy it reports ({export_id, path, bytes, files}) or null when nothing was saved.
  const finish = async (withSave, note, saved) => {
    invalidateAll()
    if (!withSave) {
      toast('success', deletedText(name, note))
      return
    }
    let copy = null
    try {
      const res = await api('GET', '/exports?instance_id=' + encodeURIComponent(instance.id))
      const list = res && Array.isArray(res.exports) ? res.exports : []
      copy = (saved && saved.export_id && list.find(e => e.id === saved.export_id)) || (saved ? list[0] : null) || null
    } catch (_) {
      copy = null
    }
    if (!copy && saved && saved.path) copy = { id: saved.export_id, local_path: saved.path, bytes: saved.bytes, files: saved.files, source_path: workdir }
    // The notice always says what happened to the files, also when nothing needed saving.
    $deleted.set({ name, instance_id: instance.id, message: note || '', export: copy })
    const said = note || (copy ? 'The files in ' + (copy.source_path || workdir) + ' were saved to ' + copy.local_path : '')
    toast('success', deletedText(name, said), {
      id: 'cc-deleted-' + instance.id,
      action: copy ? { label: 'Copy path', onClick: () => ctx.os.writeClipboard(copy.local_path) } : undefined
    })
  }

  const remove = async withSave => {
    const res = await act.run(async () => {
      const rec = await api('POST', '/instances/' + instance.id + '/destroy', { save: withSave })
      return { note: backendNote(rec), saved: rec && rec.saved }
    })
    if (!res) return false
    await finish(withSave, res.note, res.saved)
    onClose()
    return true
  }

  if (step === 'confirm') {
    return h(ConfirmDialog, {
      open: true,
      destructive: true,
      title: 'Delete ' + name + ' without saving?',
      description: 'Everything in it is deleted and cannot be recovered. No copy of ' + (known ? 'the files in ' : '') + workdir + ' is made on this computer.',
      confirmLabel: 'Delete without saving',
      onClose: () => setStep('ask'),
      onConfirm: async () => {
        let rec
        try {
          rec = await api('POST', '/instances/' + instance.id + '/destroy', { save: false })
        } catch (e) {
          throw new Error(errInfo(e).message)
        }
        await finish(false, backendNote(rec))
        onClose()
      }
    })
  }

  const notSaved = act.code === 'not_saved'
  // The backend's message already says that nothing was deleted and how to remove it
  // anyway; the dialog adds the sentence only when that advice is missing.
  const adviceGiven = /delete without saving|nothing was deleted/i.test(String(act.error || ''))
  return h(
    Modal,
    { open: true, onClose, title: 'Delete ' + name + '?', description: 'Deletes ' + name + ' and everything in it.' },
    h(
      'div',
      { className: 'flex flex-col gap-3' },
      h(
        'label',
        { className: cx('flex items-start gap-2', SMALL) },
        h(Checkbox, { checked: save, onCheckedChange: v => setSave(v === true), disabled: act.busy }),
        h('span', { className: 'flex flex-col' }, h('span', { className: cx('font-medium', SECONDARY) }, 'Save ' + (known ? 'the files in ' + workdir : workdir + ' (not recorded)') + ' to this computer first (recommended)'), h('span', { className: MUTED }, known ? 'Files outside ' + workdir + ' are not saved.' : 'Only the work folder is saved.'), h('span', { className: MUTED }, 'If saving fails, nothing is deleted.'))
      ),
      known ? null : h('div', { className: cx(SMALL, SECONDARY) }, 'The work folder of ' + name + ' is not recorded, so Own Compute cannot tell which folder to save and Delete is refused. Save the folder you need first (Files), then delete without saving.'),
      !save ? h('div', { className: cx('text-destructive', SMALL), role: 'alert' }, 'Without a saved copy, everything in it is lost for good.') : null,
      notSaved
        ? h(
            'div',
            { className: 'flex flex-col gap-2 rounded-md border border-(--ui-stroke-secondary) p-2' },
            h(ErrorLine, { message: act.error }),
            adviceGiven ? null : h('div', { className: cx(SMALL, SECONDARY) }, 'Nothing was deleted. To remove it anyway, files included, use the button below.'),
            h('div', {}, h(Button, { size: 'sm', variant: 'destructive', onClick: () => setStep('confirm') }, 'Delete without saving'))
          )
        : h(ErrorLine, { message: act.error }),
      h(
        'div',
        { className: 'flex justify-end gap-2' },
        h(Button, { variant: 'ghost', onClick: onClose }, 'Cancel'),
        save
          ? h(Button, { variant: 'destructive', disabled: act.busy, onClick: () => remove(true) }, act.busy ? 'Saving and deleting\u2026' : 'Delete')
          : h(Button, { variant: 'destructive', onClick: () => setStep('confirm') }, 'Delete without saving')
      )
    )
  )
}

// Shown on the Overview tab after a delete that saved a copy first.
function DeletedNotice() {
  const info = useValue($deleted)
  if (!info || (!info.export && !info.message)) return null
  const ex = info.export
  const close = () => $deleted.set(null)
  const src = (ex && ex.source_path) || DEFAULT_WORKDIR
  const pathInText = !!(ex && ex.local_path && String(info.message || '').indexOf(ex.local_path) >= 0)
  return h(
    Modal,
    {
      open: true,
      onClose: close,
      title: info.name + ' was deleted',
      description: info.message || 'The files in ' + src + ' were saved to this computer first. Files outside it were not saved.',
      footer: h(Button, { size: 'sm', onClick: close }, 'Close')
    },
    ex
      ? h(
          'div',
          { className: 'flex flex-col gap-2' },
          info.message ? h('div', { className: cx(SMALL, MUTED) }, 'Files outside ' + src + ' were not saved.') : null,
          // The sentence above already names the path: only Copy / Show in folder are added.
          pathInText
            ? h('div', { className: 'flex flex-wrap items-center gap-2' }, h(CopyButton, { text: ex.local_path, label: 'Copy path', size: 'sm' }), canReveal() ? h(Button, { size: 'xs', variant: 'ghost', onClick: () => revealFolder(ex.local_path) }, 'Show in folder') : null)
            : h(Fragment, {}, h('div', { className: cx(SMALL, MUTED) }, 'Saved to'), h(PathLine, { path: ex.local_path })),
          h('div', { className: cx(SMALL, SECONDARY) }, (ex.files > 0 ? sizeText(ex.bytes, ex.files) : fmtBytes(ex.bytes)) + ' \u00b7 from ' + src),
          h('div', { className: cx(SMALL, MUTED) }, 'Restore it into any environment from the Files dialog (Saved copies).')
        )
      : h('div', { className: cx(SMALL, MUTED) }, 'No copy was made, so there is nothing to restore. Files outside ' + DEFAULT_WORKDIR + ' are never saved.')
  )
}

// ---- Files ----------------------------------------------------------------------

// ---- Saving files to this computer ------------------------------------------------
// Save all / Download first measure the folder (GET .../files/size) and show '<size>, <n> files'
// before anything is transferred. A folder without files is information, not an error.

const LARGE_SAVE_BYTES = 200 * 1024 * 1024

function sizeText(bytes, files) {
  const parts = [fmtBytes(bytes)]
  if (Number.isFinite(files)) parts.push(files + ' ' + plural(files, 'file', 'files'))
  return parts.join(', ')
}

function canReveal() {
  return !!(ctx && ctx.os && typeof ctx.os.revealPath === 'function')
}

function revealFolder(path) {
  try {
    Promise.resolve(ctx.os.revealPath(path)).catch(() => toast('warning', "Couldn't open the folder. Copy the path instead."))
  } catch (_) {
    toast('warning', "Couldn't open the folder. Copy the path instead.")
  }
}

// A path on this computer with Copy and, where the system allows it, Show in folder.
function PathLine({ path }) {
  return h(
    'div',
    { className: 'flex flex-wrap items-center gap-2' },
    h('code', { className: 'break-all' }, path),
    h(CopyButton, { text: path, label: 'Copy', size: 'sm' }),
    canReveal() ? h(Button, { size: 'xs', variant: 'ghost', onClick: () => revealFolder(path) }, 'Show in folder') : null
  )
}

function SaveResult({ rec, workdir, folder }) {
  const note = backendNote(rec)
  const files = Number.isFinite(rec.files) && rec.files > 0 ? rec.files + ' ' + plural(rec.files, 'file', 'files') : 'the files'
  // The folder was measured before saving; the saved copy is a compressed archive, so the two
  // sizes differ. Say which is which.
  const sizes = 'The saved copy is ' + fmtBytes(rec.bytes) + ' (compressed)' + (folder && Number.isFinite(folder.bytes) ? '; the folder held ' + fmtBytes(folder.bytes) : '') + '.'
  return h(
    'div',
    { className: cx('flex flex-col gap-1.5 rounded-md border border-(--ui-stroke-secondary) p-2', SMALL) },
    h('div', { className: 'font-medium' }, 'Saved ' + files + ' from ' + (rec.source_path || workdir) + ' to this computer'),
    h('div', { className: MUTED }, sizes),
    h(PathLine, { path: rec.local_path }),
    rec.extracted_to ? h('div', { className: 'flex flex-col gap-1' }, h('span', { className: MUTED }, 'Also unpacked into this new folder'), h(PathLine, { path: rec.extracted_to })) : null,
    note ? h('div', { className: SECONDARY }, note) : null
  )
}

function SaveDialog({ instance, path, folder, onClose }) {
  const key = useQueryKey()
  const name = instance.name || instance.id
  const workdir = useWorkdir(instance) || DEFAULT_WORKDIR
  const [nonce] = useState(() => Date.now())
  const [dest, setDest] = useState('')
  const [unpack, setUnpack] = useState(false)
  const [done, setDone] = useState(null)
  const [nothing, setNothing] = useState('')
  const act = useAction()
  const sizeQ = useQuery({
    queryKey: key('files-size', instance.id, path, nonce),
    queryFn: () => api('GET', '/instances/' + instance.id + '/files/size?path=' + encodeURIComponent(path)),
    retry: false
  })
  const size = sizeQ.data
  const sizeErr = sizeQ.isError || sizeQ.status === 'error' ? errInfo(sizeQ.error) : null
  const known = !!size && size.known === true
  const empty = known && size.files === 0
  const large = known && Number.isFinite(size.bytes) && size.bytes > LARGE_SAVE_BYTES
  const where = cleanPath(dest)

  const save = () =>
    act.run(async () => {
      const bad = specialPathError(dest)
      if (bad) throw new Error(bad)
      const body = { path }
      if (where) body.dest_dir = where
      if (unpack) body.extract = true
      try {
        setDone(await api('POST', '/instances/' + instance.id + '/files/fetch', body))
        invalidateAll()
      } catch (e) {
        const info = errInfo(e)
        if (info.code === 'nothing_to_save') {
          setNothing(info.message)
          return
        }
        throw e
      }
    })

  let measure
  if (sizeErr) measure = h(ErrorLine, { message: fileErrorText(sizeErr) })
  else if (size === undefined) measure = h(Loading, { label: 'Measuring ' + path + '\u2026' })
  else if (known) {
    measure = h(
      'div',
      { className: cx('flex flex-col gap-1 rounded-md border border-(--ui-stroke-secondary) p-2', SMALL) },
      h('div', { className: 'font-medium' }, empty ? path + ' has no files' : path + ' holds ' + sizeText(size.bytes, size.files)),
      large ? h('div', { className: 'text-destructive' }, 'This is a large transfer (more than ' + fmtBytes(LARGE_SAVE_BYTES) + '). It can take a long time and needs that much free space on this computer.') : null,
      empty ? h('div', { className: SECONDARY }, 'Nothing to save: there are no files in it, hidden files included, so no copy would be made.') : null
    )
  } else {
    measure = h(
      'div',
      { className: cx('flex flex-col gap-1 rounded-md border border-(--ui-stroke-secondary) p-2', SMALL) },
      h('div', { className: 'font-medium' }, 'The size of ' + path + ' is not known'),
      size.reason ? h('div', { className: SECONDARY }, size.reason) : null,
      h('div', { className: 'text-destructive' }, 'Without a size there is no way to say how much will be transferred. A large folder can take a long time and fill the disk on this computer.')
    )
  }

  return h(
    Modal,
    { open: true, onClose, title: 'Save ' + path + ' from ' + name, description: 'Copies the files to this computer. The environment is not changed.' },
    done
      ? h('div', { className: 'flex flex-col gap-2' }, h(SaveResult, { rec: done, workdir, folder: known ? size : null }), h('div', {}, h(Button, { size: 'sm', onClick: onClose }, 'Close')))
      : nothing
        ? h(
            'div',
            { className: 'flex flex-col gap-2' },
            h('div', { className: cx('rounded-md border border-(--ui-stroke-secondary) p-2', SMALL, SECONDARY) }, nothing),
            h('div', {}, h(Button, { size: 'sm', onClick: onClose }, 'Close'))
          )
        : h(
            'div',
            { className: 'flex flex-col gap-3' },
            measure,
            empty
              ? null
              : h(
                  Fragment,
                  {},
                  h(TextField, { label: 'Save into folder (optional)', value: dest, onChange: setDest, placeholder: folder || 'The saved-copies folder', hint: folder ? 'Blank saves into ' + folder + ' (the exports_dir setting). Give a folder on this computer, for example a project folder, to save there instead.' : 'Blank saves into the saved-copies folder. Give a folder on this computer to save there instead.' }),
                  h(
                    'label',
                    { className: cx('flex items-start gap-2', SMALL) },
                    h(Checkbox, { checked: unpack, onCheckedChange: v => setUnpack(v === true), disabled: act.busy }),
                    h('span', { className: 'flex flex-col' }, h('span', { className: cx('font-medium', SECONDARY) }, 'Unpack into a new subfolder'), h('span', { className: MUTED }, 'Also unpacks the files into a new folder next to the archive. Existing files there are never overwritten.'))
                  )
                ),
            h(ErrorLine, { message: act.error ? fileErrorText({ code: act.code, status: act.status, message: act.error }) : null }),
            h(
              'div',
              { className: 'flex justify-end gap-2' },
              h(Button, { variant: 'ghost', onClick: onClose }, empty ? 'Close' : 'Cancel'),
              empty ? null : h(Button, { disabled: act.busy || (size === undefined && !sizeErr), onClick: save }, act.busy ? 'Saving\u2026' : known && !large ? 'Save ' + sizeText(size.bytes, size.files) : 'Save anyway')
            )
          )
  )
}

function ExportRow({ item, onRestore, onDelete }) {
  return h(
    'li',
    { className: cx('flex flex-col gap-1 rounded-md border border-(--ui-stroke-secondary) px-2.5 py-1.5', SMALL) },
    h(
      'div',
      { className: 'flex flex-wrap items-center justify-between gap-2' },
      h('span', { className: 'font-medium' }, 'Saved ' + ago(item.created_at) + ' \u00b7 ' + (item.files > 0 ? sizeText(item.bytes, item.files) : fmtBytes(item.bytes)) + ' \u00b7 ' + (EXPORT_REASONS[item.reason] || item.reason || 'saved')),
      h('span', { className: 'flex gap-1.5' }, h(Button, { size: 'xs', variant: 'outline', onClick: () => onRestore(item) }, 'Restore into\u2026'), h(Button, { size: 'xs', variant: 'ghost', onClick: () => onDelete(item) }, 'Delete'))
    ),
    h('div', { className: cx('flex flex-col gap-1', MUTED) }, h('span', {}, 'From ' + (item.source_path || DEFAULT_WORKDIR) + ' of ' + (item.instance_name || item.instance_id)), h(PathLine, { path: item.local_path })),
    item.extracted_to ? h('div', { className: cx('flex flex-col gap-1', MUTED) }, h('span', {}, 'Also unpacked into a folder, which stays when this copy is deleted'), h(PathLine, { path: item.extracted_to })) : null
  )
}

function RestoreDialog({ item, current, onClose }) {
  const key = useQueryKey()
  const instQ = useQuery({ queryKey: key('instances'), queryFn: () => api('GET', '/instances'), retry: 1 })
  const list = (Array.isArray(instQ.data) ? instQ.data : []).filter(i => i && i.status !== 'destroyed')
  const [target, setTarget] = useState(current ? current.id : '')
  const [dest, setDest] = useState(DEFAULT_WORKDIR)
  const [done, setDone] = useState(null)
  const [asking, setAsking] = useState(false)
  const chosen = list.find(i => i.id === target) || null
  const folder = cleanPath(dest) || DEFAULT_WORKDIR
  const targetName = (chosen && (chosen.name || chosen.id)) || target
  const copyName = 'the copy saved ' + ago(item.created_at) + ' (' + fmtBytes(item.bytes) + ', from ' + (item.source_path || DEFAULT_WORKDIR) + ' of ' + (item.instance_name || item.instance_id) + ')'
  // Nothing is sent until the user confirms: restoring overwrites files for good.
  const restore = async () => {
    let res
    try {
      res = await api('POST', '/exports/' + encodeURIComponent(item.id) + '/restore', { instance_id: target, dest: folder })
    } catch (e) {
      throw new Error(fileErrorText(errInfo(e)))
    }
    setDone(res || {})
    toast('success', 'Restored the saved copy into ' + targetName + '.')
    invalidateAll()
  }
  return h(
    Modal,
    { open: true, onClose, title: 'Restore a saved copy', description: 'Unpacks the copy saved ' + ago(item.created_at) + ' into an environment. Files with the same names there are replaced.' },
    done
      ? h(
          'div',
          { className: 'flex flex-col gap-2' },
          h('div', { className: cx(SMALL, SECONDARY) }, 'Restored into ' + (done.dest || dest) + ' of ' + ((chosen && chosen.name) || target) + (Number.isFinite(done.bytes) ? ' (' + fmtBytes(done.bytes) + ')' : '') + '.'),
          h('div', {}, h(Button, { size: 'sm', onClick: onClose }, 'Close'))
        )
      : h(
          'div',
          { className: 'flex flex-col gap-3' },
          h(Field, { label: 'Environment' }, h(Pick, { value: target, onChange: setTarget, placeholder: 'Choose an environment', options: list.map(i => ({ value: i.id, label: (i.name || i.id) + ' (' + (i.status || 'unknown') + ')' })) })),
          h(TextField, { label: 'Destination folder', value: dest, onChange: setDest, hint: 'A folder inside the environment. It is created if it does not exist.' }),
          h('div', { className: 'flex justify-end gap-2' }, h(Button, { variant: 'ghost', onClick: onClose }, 'Cancel'), h(Button, { disabled: !target, onClick: () => setAsking(true) }, 'Restore\u2026')),
          asking
            ? h(ConfirmDialog, {
                open: true,
                destructive: true,
                title: 'Replace files in ' + folder + '?',
                description: 'Restores ' + copyName + ' into ' + folder + ' of ' + targetName + '. Files in ' + folder + ' with the same names are replaced and cannot be recovered.',
                confirmLabel: 'Restore and replace',
                onClose: () => setAsking(false),
                onConfirm: restore
              })
            : null
        )
  )
}

function UploadPanel({ instance, defaultDest, onClose, onUploaded }) {
  const name = instance.name || instance.id
  const [local, setLocal] = useState('')
  const [dest, setDest] = useState(defaultDest || DEFAULT_WORKDIR)
  const [asking, setAsking] = useState(false)
  const [pathError, setPathError] = useState('')
  const askFirst = () => {
    const bad = specialPathError(local)
    setPathError(bad)
    if (!bad) setAsking(true)
  }
  return h(
    'div',
    { className: cx(CARD) },
    h('div', { className: 'text-sm font-semibold' }, 'Upload to ' + name),
    h(TextField, { label: 'Path on this computer', value: local, onChange: setLocal, placeholder: 'A file or folder, as a full path', hint: 'Files in private folders (SSH keys, Hermes data) are never uploaded. Quotes around the path are fine.' }),
    h(TextField, { label: 'Destination folder in the environment', value: dest, onChange: setDest }),
    h(ErrorLine, { message: pathError }),
    h('div', { className: 'flex gap-2' }, h(Button, { size: 'sm', disabled: !cleanPath(local), onClick: askFirst }, 'Upload\u2026'), h(Button, { size: 'sm', variant: 'ghost', onClick: onClose }, 'Cancel')),
    asking
      ? h(ConfirmDialog, {
          open: true,
          title: 'Send files to ' + name + '?',
          description: 'This sends ' + cleanPath(local) + ' from this computer to ' + (cleanPath(dest) || DEFAULT_WORKDIR) + ' in ' + name + '. Files with the same names there are replaced.',
          confirmLabel: 'Upload',
          onClose: () => setAsking(false),
          onConfirm: async () => {
            let res
            try {
              res = await api('POST', '/instances/' + instance.id + '/files/put', { local_path: cleanPath(local), dest: cleanPath(dest) || DEFAULT_WORKDIR })
            } catch (e) {
              const info = errInfo(e)
              throw new Error(fileErrorText(info))
            }
            toast('success', 'Uploaded to ' + ((res && res.dest) || dest) + ' in ' + name + '.')
            invalidateAll()
            onUploaded(res || {})
          }
        })
      : null
  )
}

function FilesDialog({ instance, onClose }) {
  const key = useQueryKey()
  const name = instance.name || instance.id
  const running = instance.status === 'running'
  const [path, setPath] = useState(null)
  const [saving, setSaving] = useState(null)
  const [panel, setPanel] = useState(null)
  const [restoring, setRestoring] = useState(null)
  const [deleting, setDeleting] = useState(null)
  const act = useAction()
  const workdir = useWorkdir(instance) || DEFAULT_WORKDIR
  const listQ = useQuery({
    queryKey: key('files', instance.id, path || ''),
    queryFn: () => api('GET', '/instances/' + instance.id + '/files' + (path ? '?path=' + encodeURIComponent(path) : '')),
    enabled: running,
    retry: false
  })
  const expQ = useQuery({ queryKey: key('exports', instance.id), queryFn: () => api('GET', '/exports?instance_id=' + encodeURIComponent(instance.id)), retry: 1 })

  // Save all / Download measure first, then ask (SaveDialog).
  const download = target => setSaving(target)

  const listing = listQ.data
  const here = (listing && listing.path) || path || workdir
  const entries = listing && Array.isArray(listing.entries) ? listing.entries : []
  const exportsData = expQ.data || {}
  const exportList = Array.isArray(exportsData.exports) ? exportsData.exports : []
  const folder = typeof exportsData.folder === 'string' ? exportsData.folder : ''
  const listInfo = listQ.isError || listQ.status === 'error' ? errInfo(listQ.error) : null

  return h(
    Modal,
    { open: true, onClose, title: 'Files: ' + name, className: 'max-h-[85vh] overflow-y-auto', description: 'Browse the environment, download files to this computer, upload files, and manage saved copies.' },
    h(
      'div',
      { className: 'flex flex-col gap-4' },
      h(
        Section,
        {
          title: 'In the environment',
          actions: h(
            'span',
            { className: 'flex gap-1.5' },
            h(Button, { size: 'xs', variant: 'outline', disabled: act.busy, onClick: () => download(workdir) }, 'Save all of ' + workdir),
            h(Button, { size: 'xs', variant: 'outline', onClick: () => setPanel(panel === 'upload' ? null : 'upload') }, 'Upload\u2026')
          )
        },
        h(ErrorLine, { message: act.error ? fileErrorText({ code: act.code, status: act.status, message: act.error }) : null }),
        panel === 'upload'
          ? h(UploadPanel, {
              instance,
              defaultDest: here,
              onClose: () => setPanel(null),
              onUploaded: () => {
                setPanel(null)
                listQ.refetch && listQ.refetch()
              }
            })
          : null,
        !running
          ? h('div', { className: cx(SMALL, SECONDARY) }, 'Start it to browse files. Saving all of ' + workdir + ' and the saved copies below still work.')
          : listInfo
            ? h('div', { className: 'flex flex-col gap-2' }, h(ErrorLine, { message: fileErrorText(listInfo) }), h('div', {}, h(Button, { size: 'xs', variant: 'outline', onClick: () => setPath(null) }, 'Back to ' + workdir)))
            : listQ.data === undefined
              ? h(Loading, { label: 'Loading files\u2026' })
              : h(
                  'div',
                  { className: 'flex flex-col gap-1.5' },
                  h('nav', { className: 'flex flex-wrap items-center gap-1', 'aria-label': 'Folder path' }, pathCrumbs(here).map(c => h(Button, { key: c.path, size: 'xs', variant: c.path === here ? 'secondary' : 'ghost', onClick: () => setPath(c.path) }, c.label))),
                  entries.length
                    ? h(
                        'ul',
                        { className: 'flex flex-col gap-1' },
                        entries.map(e =>
                          h(
                            'li',
                            { key: e.name, className: cx('flex flex-wrap items-center justify-between gap-2 rounded-md border border-(--ui-stroke-secondary) px-2.5 py-1', SMALL) },
                            e.type === 'dir'
                              ? h(Button, { size: 'xs', variant: 'ghost', onClick: () => setPath(joinPath(here, e.name)) }, e.name + '/')
                              : h('span', { className: 'font-medium' }, e.name),
                            h('span', { className: cx('flex items-center gap-3', MUTED) }, h('span', {}, fileFacts(e)), h(Button, { size: 'xs', variant: 'outline', disabled: act.busy, onClick: () => download(joinPath(here, e.name)) }, 'Download'))
                          )
                        )
                      )
                    : h('div', { className: cx(SMALL, MUTED) }, 'This folder is empty.'),
                  listing && listing.truncated ? h('div', { className: cx(SMALL, MUTED) }, 'Only the first entries are shown. Open a subfolder or download the whole folder.') : null
                )
      ),
      h(
        Section,
        { title: 'Saved copies' },
        h('div', { className: cx(SMALL, MUTED) }, 'A saved copy holds one folder, usually ' + workdir + '. Files elsewhere in the environment are not included.'),
        folder ? h('div', { className: cx('flex flex-wrap items-center gap-2', SMALL, SECONDARY) }, h('span', {}, 'Stored in'), h('code', { className: 'break-all' }, folder), h(CopyButton, { text: folder, label: 'Copy', size: 'sm' })) : null,
        expQ.data === undefined && !expQ.isError
          ? h(Loading, { label: 'Loading saved copies\u2026' })
          : exportList.length
            ? h('ul', { className: 'flex flex-col gap-1.5' }, exportList.map(it => h(ExportRow, { key: it.id, item: it, onRestore: setRestoring, onDelete: setDeleting })))
            : h('div', { className: cx(SMALL, MUTED) }, 'No saved copies of this environment yet. Delete saves one automatically, and Save all of ' + workdir + ' makes one on request.')
      )
    ),
    restoring ? h(RestoreDialog, { item: restoring, current: instance, onClose: () => setRestoring(null) }) : null,
    saving ? h(SaveDialog, { instance, path: saving, folder, onClose: () => setSaving(null) }) : null,
    deleting
      ? h(ConfirmDialog, {
          open: true,
          destructive: true,
          title: 'Delete this saved copy?',
          description: 'The copy saved ' + ago(deleting.created_at) + ' (' + fmtBytes(deleting.bytes) + ') is removed from this computer: ' + deleting.local_path + '. It cannot be recovered.' + (deleting.extracted_to ? ' The folder it was unpacked into (' + deleting.extracted_to + ') is not removed.' : ''),
          confirmLabel: 'Delete',
          onClose: () => setDeleting(null),
          onConfirm: async () => {
            let res
            try {
              res = await api('DELETE', '/exports/' + encodeURIComponent(deleting.id))
            } catch (e) {
              throw new Error(errInfo(e).message)
            }
            toast('success', backendNote(res) || 'Deleted the saved copy.')
            invalidateAll()
          }
        })
      : null
  )
}

// ---- Environment card -------------------------------------------------------------

// Container ports a template publishes, as typed ('8000, 3000'). Checked like the backend
// does (core/templates.py): 1-65535, at most 10, no duplicates, not the screen's 6080.
const MAX_PUBLISHED_PORTS = 10

function validatePorts(textValue, display) {
  const parts = String(textValue == null ? '' : textValue)
    .split(/[\s,;]+/)
    .filter(Boolean)
  const out = []
  if (parts.length > MAX_PUBLISHED_PORTS) return { error: 'Published ports can list at most ' + MAX_PUBLISHED_PORTS + ' container ports.' }
  for (const part of parts) {
    const n = /^\d+$/.test(part) ? Number(part) : NaN
    if (!Number.isInteger(n) || n < 1 || n > 65535) return { error: 'Port ' + part + ' is not valid: use a container port number from 1 to 65535.' }
    if (out.indexOf(n) >= 0) return { error: 'Port ' + n + ' is listed twice.' }
    if (n === DISPLAY_PORT && display) return { error: 'Port ' + DISPLAY_PORT + ' is used by the screen; remove it from the ports or turn Show a screen off.' }
    out.push(n)
  }
  return { ports: out }
}

// Ports the environment's template published (not the screen), from the instance record:
// { '8000': '127.0.0.1:45557' } on the host. Forward brings one to this computer.
function publishedPorts(instance) {
  const ports = instance && instance.ports && typeof instance.ports === 'object' ? instance.ports : {}
  return Object.keys(ports)
    .filter(k => /^\d+$/.test(k))
    .map(k => ({ port: Number(k), host: ports[k] }))
    .sort((a, b) => a.port - b.port)
}

function PublishedPorts({ instance }) {
  const list = publishedPorts(instance)
  const act = useAction()
  const [opened, setOpened] = useState(null)
  if (!list.length || instance.status !== 'running') return null
  const forward = (port, open) =>
    act.run(async () => {
      const rec = await api('POST', '/instances/' + instance.id + '/forward', { remote_port: port })
      const url = (rec && rec.url) || LOOPBACK_SCHEME + '127.0.0.1:' + (rec && rec.local_port)
      setOpened({ port, url })
      invalidateAll()
      if (open) await ctx.os.openExternal(url)
      else toast('info', 'Port ' + port + ' of ' + (instance.name || instance.id) + ' is forwarded to ' + maskUrl(url) + ' on this computer.')
    })
  return h(
    'div',
    { className: cx('flex flex-col gap-1.5 rounded-md border border-(--ui-stroke-secondary) p-2', SMALL) },
    h('div', { className: 'font-medium' }, 'Published ports'),
    list.map(p =>
      h(
        'div',
        { key: p.port, className: 'flex flex-wrap items-center justify-between gap-2' },
        h('span', {}, 'Port ' + p.port + ' \u2192 ' + p.host + ' on the host'),
        h(
          'span',
          { className: 'flex gap-1.5' },
          h(Button, { size: 'xs', variant: 'outline', disabled: act.busy, onClick: () => forward(p.port, false) }, 'Forward'),
          h(Button, { size: 'xs', variant: 'outline', disabled: act.busy, onClick: () => forward(p.port, true) }, 'Open in browser')
        )
      )
    ),
    opened ? h('div', { className: cx('flex flex-wrap items-center gap-2', SECONDARY) }, 'Port ' + opened.port + ' is on this computer at', h('code', {}, maskUrl(opened.url)), h(CopyButton, { text: opened.url, label: 'Copy', size: 'sm' })) : null,
    opened ? h(RemoteNote, { port: portOfAddress({ url: opened.url }) }) : null,
    h(ErrorLine, { message: act.error })
  )
}

// A refused Screen (needs_setup) points at the template to change.
function ScreenFix({ code }) {
  if (code !== 'needs_setup') return null
  return h(
    'div',
    { className: cx('flex flex-wrap items-center gap-2', SMALL, SECONDARY) },
    'Use the Desktop (noVNC) template for an environment with a screen, or turn off Show a screen in the template.',
    h(Button, { size: 'xs', variant: 'outline', onClick: () => setIntent('templates') }, 'Open Templates')
  )
}

function InstanceCard(props) {
  const p = props || {}
  const instance = p.instance || p.item || {}
  const actions = instanceActions(instance)
  const act = useAction()
  const term = useTerminalCooldown()
  const intent = useValue($intent)
  const [dialog, setDialog] = useState(null)
  const stats = instance.stats || null
  const limit = timeLimitInfo(instance)
  const reason = stopReasonText(instance)
  const expiredStart = startNeedsNotice(instance)
  const name = instance.name || instance.id

  // The expiry warning toast sends the user here with an Extend request.
  useEffect(() => {
    if (intent && intent.action === 'extend' && intent.instanceId === instance.id) {
      setDialog('extend')
      $intent.set(null)
    }
  }, [intent])

  const metric = []
  if (stats) {
    metric.push(Number.isFinite(stats.cpu_pct) ? 'CPU ' + Math.round(stats.cpu_pct) + '%' : 'CPU unknown')
    metric.push('Memory ' + fmtMb(stats.mem_used_mb) + ' / ' + fmtMb(stats.mem_limit_mb))
  }
  if (Number.isFinite(instance.idle_minutes) && !instanceAsleep(instance)) metric.push('Stops after ' + instance.idle_minutes + ' min idle')
  if (instance.last_activity_at) metric.push('Last used ' + ago(instance.last_activity_at))

  const tone = instance.status === 'running' ? 'good' : instance.status === 'creating' ? 'warn' : instance.status === 'error' ? 'bad' : 'muted'
  const lifecycle = verb =>
    act.run(async () => {
      await api('POST', '/instances/' + instance.id + '/' + verb)
      invalidateAll()
    })

  const screen = () =>
    act.run(async () => {
      const rec = await openScreen(instance)
      if (rec && rec.url) {
        toast('info', 'Opened the screen for ' + name + '.', {
          action: { label: 'Open in browser', onClick: () => ctx.os.openExternal(rec.url) }
        })
      }
    })

  const buttons = {
    terminal: h(Tip, { key: 'terminal', label: COPY.tooltips.terminal + ' ' + REMOTE_TERMINAL_NOTE }, h(Button, { size: 'xs', variant: 'outline', disabled: act.busy || term.cooling, onClick: () => { if (term.cooling) return undefined; term.start(); return act.run(() => openTerminal({ kind: 'instance', id: instance.id, name })) } }, 'Terminal')),
    screen: h(Tip, { key: 'screen', label: COPY.tooltips.screen }, h(Button, { size: 'xs', variant: 'outline', disabled: act.busy, onClick: screen }, 'Screen')),
    forward: h(Tip, { key: 'forward', label: COPY.tooltips.forward_port }, h(Button, { size: 'xs', variant: 'outline', onClick: () => setDialog('forward') }, 'Forward port\u2026')),
    logs: h(Button, { key: 'logs', size: 'xs', variant: 'outline', onClick: () => setDialog('logs') }, 'Logs'),
    files: h(Tip, { key: 'files', label: 'Browse, download and upload files, and manage saved copies of this environment.' }, h(Button, { size: 'xs', variant: 'outline', onClick: () => setDialog('files') }, 'Files\u2026')),
    extend: h(Tip, { key: 'extend', label: 'Adds time before the time limit stops this environment.' }, h(Button, { size: 'xs', variant: 'outline', onClick: () => setDialog('extend') }, 'Extend\u2026')),
    stop: h(Button, { key: 'stop', size: 'xs', variant: 'outline', disabled: act.busy, onClick: () => lifecycle('stop') }, 'Stop'),
    start: h(Button, { key: 'start', size: 'xs', variant: 'outline', disabled: act.busy, onClick: () => (expiredStart ? setDialog('start') : lifecycle('start')) }, 'Start'),
    destroy: h(Button, { key: 'destroy', size: 'xs', variant: 'ghost', onClick: () => setDialog('destroy') }, 'Delete'),
    chat: h(Button, { key: 'chat', size: 'xs', variant: 'ghost', disabled: act.busy, onClick: () => act.run(() => sendToChat(instance)) }, 'Use in chat')
  }

  return h(
    'div',
    { className: CARD },
    h(
      'div',
      { className: 'flex flex-wrap items-center justify-between gap-2' },
      h(
        'div',
        { className: 'flex min-w-0 items-center gap-2' },
        h(StatusDot, { tone }),
        h('span', { className: 'truncate text-sm font-semibold' }, name),
        h(Badge, { variant: instance.status === 'error' ? 'destructive' : 'muted' }, instance.status || 'unknown'),
        limit && limit.soon ? h(Badge, { variant: 'warn' }, limit.left ? 'Time limit in ' + fmtDuration(limit.left) : 'Time limit reached') : null
      ),
      h('span', { className: cx(SMALL, MUTED) }, 'Template ' + (instance.template_id || '?') + ' \u00b7 Host ' + (instance.host_id || '?'))
    ),
    limit ? h('div', { className: cx(SMALL, SECONDARY) }, limit.text) : null,
    reason ? h('div', { className: cx(SMALL, SECONDARY) }, reason) : null,
    expiredStart ? h('div', { className: cx(SMALL, MUTED) }, 'Starting it gives it ' + freshLimitText(instance) + '.') : null,
    metric.length ? h('div', { className: cx('flex flex-wrap gap-x-4 gap-y-0.5', SMALL, SECONDARY) }, metric.map(m => h('span', { key: m }, m))) : null,
    instance.display === true && instance.display_state === 'unsupported'
      ? h('div', { className: cx(SMALL, MUTED) }, COPY.tooltips.screen_disabled)
      : null,
    instance.display === false ? h('div', { className: cx(SMALL, MUTED) }, COPY.tooltips.screen_not_enabled) : null,
    instance.error ? h('div', { className: cx('text-destructive', SMALL) }, maskText(instance.error)) : null,
    h(ErrorLine, { message: act.error }),
    h(ScreenFix, { code: act.code }),
    h(PublishedPorts, { instance }),
    h('div', { className: 'flex flex-wrap gap-1.5' }, actions.map(a => buttons[a])),
    dialog === 'forward' ? h(ForwardDialog, { instance, onClose: () => setDialog(null) }) : null,
    dialog === 'logs' ? h(LogsDialog, { instance, onClose: () => setDialog(null) }) : null,
    dialog === 'files' ? h(FilesDialog, { instance, onClose: () => setDialog(null) }) : null,
    dialog === 'extend' ? h(ExtendDialog, { instance, onClose: () => setDialog(null) }) : null,
    dialog === 'destroy' ? h(DeleteDialog, { instance, onClose: () => setDialog(null) }) : null,
    dialog === 'start'
      ? h(ConfirmDialog, {
          open: true,
          title: 'Start ' + name + '?',
          description: 'Its time limit was reached, so it was stopped. Starting it gives it ' + freshLimitText(instance) + '. Files are kept.',
          confirmLabel: 'Start',
          onClose: () => setDialog(null),
          onConfirm: async () => {
            try {
              await api('POST', '/instances/' + instance.id + '/start')
            } catch (e) {
              throw new Error(errInfo(e).message)
            }
            invalidateAll()
          }
        })
      : null
  )
}

function JobRow({ job }) {
  const tone = job.state === 'RUNNING' ? 'good' : job.state === 'PENDING' ? 'warn' : 'muted'
  return h(
    'li',
    { className: cx('flex flex-wrap items-center justify-between gap-2 rounded-md border border-(--ui-stroke-secondary) px-2.5 py-1.5', SMALL) },
    h('span', { className: 'flex min-w-0 items-center gap-2' }, h(StatusDot, { tone }), h('span', { className: 'truncate font-medium' }, job.name || job.id), h(Badge, { variant: 'muted' }, jobStateLabel(job)), job.stale ? h('span', { className: cx(SMALL, MUTED) }, 'not refreshed') : null),
    h(
      'span',
      { className: MUTED },
      [job.host_id, job.partition, job.nodes, 'submitted ' + ago(job.submitted_at)].filter(Boolean).join(' \u00b7 ')
    )
  )
}

function TunnelRow({ tunnel }) {
  const act = useAction()
  const isDisplay = tunnel.kind === 'display'
  const close = () =>
    act.run(async () => {
      await api('DELETE', '/tunnels/' + encodeURIComponent(tunnel.id))
      toast('success', 'Closed the ' + (isDisplay ? 'screen' : 'port') + ' tunnel.')
      invalidateAll()
    })
  return h(
    'li',
    { className: cx('flex flex-col gap-1 rounded-md border border-(--ui-stroke-secondary) px-2.5 py-1.5', SMALL) },
    h(
      'div',
      { className: 'flex flex-wrap items-center justify-between gap-2' },
      h(
        'span',
        { className: 'flex min-w-0 items-center gap-2' },
        h(Badge, { variant: 'muted' }, tunnel.kind || 'port'),
        h('code', { className: 'truncate' }, maskUrl(tunnel.url) || '127.0.0.1:' + tunnel.local_port)
      ),
      h(
        'span',
        { className: 'flex gap-1.5' },
        isDisplay && tunnel.url ? h(Button, { size: 'xs', variant: 'outline', onClick: () => ctx.os.openExternal(tunnel.url) }, 'Open in browser') : null,
        h(Button, { size: 'xs', variant: 'ghost', disabled: act.busy, onClick: close }, 'Close')
      )
    ),
    h(
      'div',
      { className: cx('flex flex-wrap gap-x-4', MUTED) },
      h('span', {}, 'Local port ' + tunnel.local_port),
      tunnel.instance_id ? h('span', {}, 'Environment ' + tunnel.instance_id) : h('span', {}, 'Host ' + tunnel.host_id),
      h('span', {}, 'Expires ' + ago(tunnel.expires_at))
    ),
    h(RemoteNote, { port: portOfAddress(tunnel) }),
    h(ErrorLine, { message: act.error })
  )
}

function OverviewTab() {
  const key = useQueryKey()
  const [stopping, setStopping] = useState(false)
  // The Stopped group stays folded or open the way the user left it.
  const [stoppedFolded, setStoppedFolded] = useState(() => !!(ctx && ctx.storage && ctx.storage.get('stopped-folded', false)))
  const summaryQ = useQuery({ queryKey: key('summary'), queryFn: () => api('GET', '/summary'), refetchInterval: POLL_MS, retry: 1 })
  const instQ = useQuery({ queryKey: key('instances'), queryFn: () => api('GET', '/instances'), refetchInterval: POLL_MS, retry: 1 })
  const jobsQ = useQuery({ queryKey: key('jobs', 'active'), queryFn: () => api('GET', '/jobs?active=1'), refetchInterval: POLL_MS, retry: 1 })
  const tunnelsQ = useQuery({ queryKey: key('tunnels'), queryFn: () => api('GET', '/tunnels'), refetchInterval: POLL_MS, retry: 1 })

  const model = chipModel(summaryQ.data)
  const attention = summaryQ.data && Array.isArray(summaryQ.data.attention) ? summaryQ.data.attention : []

  return h(
    'div',
    { className: 'flex flex-col gap-4' },
    h(
      'div',
      { className: 'flex flex-wrap items-center justify-between gap-2' },
      h(
        'div',
        { className: cx('flex items-center gap-2', SMALL, SECONDARY) },
        h(StatusDot, { tone: chipTone(model.tone) }),
        h('span', {}, summaryQ.data ? model.text : 'Loading summary\u2026')
      ),
      h(
        'div',
        { className: 'flex gap-2' },
        h(Button, { size: 'sm', variant: 'outline', onClick: () => setStopping(true) }, 'Stop all'),
        h(Button, { size: 'sm', variant: 'outline', onClick: () => invalidateAll() }, 'Refresh')
      )
    ),
    attention.length
      ? h(
          'ul',
          { className: cx('flex flex-col gap-1', SMALL, SECONDARY) },
          attention.map((a, i) => h('li', { key: (a.id || '') + i }, '\u2022 ' + a.message))
        )
      : null,
    h(
      Section,
      { title: 'Environments' },
      h(Async, { q: instQ, label: 'Loading environments\u2026' }, data => {
        const list = Array.isArray(data) ? data : []
        if (!list.length) {
          return h(EmptyState, { title: 'No environments running', description: 'Spawn one from the Templates tab.' })
        }
        const groups = splitInstances(list)
        const toggle = () =>
          setStoppedFolded(v => {
            if (ctx && ctx.storage) ctx.storage.set('stopped-folded', !v)
            return !v
          })
        return h(
          'div',
          { className: 'flex flex-col gap-4' },
          groups.active.length
            ? h('div', { className: 'flex flex-col gap-3' }, groups.active.map(inst => h(InstanceCard, { key: inst.id, instance: inst })))
            : h('div', { className: cx(SMALL, MUTED) }, 'No environment is running.'),
          groups.stopped.length
            ? h(
                HostGroup,
                { group: { kind: 'stopped', title: 'Stopped', items: groups.stopped }, collapsed: stoppedFolded, onToggle: toggle, what: 'environments' },
                h('div', { className: 'flex flex-col gap-3' }, groups.stopped.map(inst => h(InstanceCard, { key: inst.id, instance: inst })))
              )
            : null
        )
      })
    ),
    h(
      Section,
      { title: 'Active jobs' },
      h(Async, { q: jobsQ, label: 'Loading jobs\u2026' }, data => {
        const list = Array.isArray(data) ? data : []
        if (!list.length) return h('div', { className: cx(SMALL, MUTED) }, 'No active jobs.')
        return h('ul', { className: 'flex flex-col gap-1.5' }, list.map(job => h(JobRow, { key: job.id, job })))
      })
    ),
    h(
      Section,
      { title: 'Open tunnels' },
      h(Async, { q: tunnelsQ, label: 'Loading tunnels\u2026' }, data => {
        const list = (Array.isArray(data) ? data : []).filter(t => t && t.status !== 'closed')
        if (!list.length) return h('div', { className: cx(SMALL, MUTED) }, 'No open tunnels.')
        return h('ul', { className: 'flex flex-col gap-1.5' }, list.map(t => h(TunnelRow, { key: t.id, tunnel: t })))
      })
    ),
    h(DeletedNotice, {}),
    stopping
      ? h(ConfirmDialog, {
          open: true,
          destructive: true,
          title: 'Stop all environments?',
          description: 'Every running environment on every host is stopped. They can be started again later.',
          confirmLabel: 'Stop all',
          onClose: () => setStopping(false),
          onConfirm: async () => {
            try {
              await api('POST', '/instances/stop-all', {})
            } catch (e) {
              throw new Error(errInfo(e).message)
            }
            toast('success', 'Stopped all environments.')
            invalidateAll()
          }
        })
      : null
  )
}

// ---------------------------------------------------------------------------
// Templates tab
// ---------------------------------------------------------------------------

// The host kinds a template can run on: the same rule as `fitting_host_kinds` in core/models.py
// (the spawn check): an EC2 machine is an ordinary SSH container host, so a template for SSH
// hosts also runs on EC2 hosts. Keep the two in step (the smoke test pins this one).
function templateKinds(template) {
  const kinds = template && Array.isArray(template.host_kinds) ? template.host_kinds : null
  if (!kinds) return null
  return kinds.indexOf('ssh') >= 0 && kinds.indexOf('ec2') < 0 ? kinds.concat(['ec2']) : kinds
}

function eligibleHosts(template, hosts) {
  const kinds = templateKinds(template)
  return (hosts || []).filter(i => i && i.spec && (!kinds || kinds.indexOf(i.spec.kind) >= 0))
}

// The name of a host in a host picker: its state, or for an EC2 host without a running machine
// what is needed first.
function hostChoiceLabel(i) {
  return (i.spec.name || i.spec.id) + ' (' + (spawnMachineNote(i).text || statusLight(i.state ? i.state.status : 'unknown').label) + ')'
}

// What a spawn on this host needs first. Only an EC2 host can lack a machine: `blocked` means
// the spawn cannot work right now (the text says why); otherwise `text` says what happens.
function spawnMachineNote(item) {
  const spec = item && item.spec
  if (!spec || spec.kind !== 'ec2') return { blocked: false, text: '' }
  const ec2 = spec.ec2 || {}
  const state = ec2.machine_state || 'none'
  if (state === 'ready') return { blocked: false, text: '' }
  if ((state === 'none' || state === 'stopped') && ec2.auto_start) return { blocked: false, text: 'starts the machine first' }
  if (state === 'none') return { blocked: true, text: 'no machine yet: start it on the Hosts tab' }
  if (state === 'stopped') return { blocked: true, text: 'machine stopped: start it on the Hosts tab' }
  if (state === 'launching' || state === 'starting') return { blocked: true, text: 'machine is starting: wait until it is ready' }
  if (state === 'stopping') return { blocked: true, text: 'machine is stopping: wait until it has stopped' }
  return { blocked: true, text: 'machine needs attention: see the Hosts tab' }
}

// The templates shipped with the plugin. A stored copy of one of these is the user's
// own version (the backend clears its `builtin` flag), so the flag alone is not enough.
const SHIPPED_TEMPLATE_IDS = ['ubuntu-dev', 'python-dev', 'desktop-novnc', 'slurm-python', 'hermes-agent', 'agent-script']

// An agent template carries an enabled `agent` object in mode hermes or script.
function isAgentTemplate(t) {
  const a = t && t.agent
  if (!a || typeof a !== 'object' || a.enabled === false) return false
  const mode = a.mode == null ? 'hermes' : a.mode
  return mode === 'hermes' || mode === 'script'
}

function stableJson(value) {
  if (Array.isArray(value)) return '[' + value.map(stableJson).join(',') + ']'
  if (value && typeof value === 'object') {
    return '{' + Object.keys(value).sort().filter(k => value[k] !== undefined).map(k => JSON.stringify(k) + ':' + stableJson(value[k])).join(',') + '}'
  }
  return JSON.stringify(value === undefined ? null : value)
}

// Equal apart from the `builtin` flag.
function sameTemplate(a, b) {
  if (!a || !b) return false
  const strip = t => Object.assign({}, t, { builtin: undefined })
  return stableJson(strip(a)) === stableJson(strip(b))
}

// `defaults` maps a template id to its shipped version (null when it has none).
function templateInfo(t, defaults) {
  const shipped = defaults && defaults[t.id] ? defaults[t.id] : null
  const builtin = !!shipped || !!t.builtin
  return { builtin, modified: !!shipped && !sameTemplate(t, shipped), shipped }
}

function variableDescription(v, template) {
  if (v && typeof v.description === 'string' && v.description) return v.description
  const own = template && Array.isArray(template.variables) ? template.variables.find(x => x && x.name === (v && v.name)) : null
  return own && typeof own.description === 'string' ? own.description : ''
}

// Variables that will leave this computer as secrets.
function secretsToSend(preview) {
  const vars = preview && Array.isArray(preview.variables) ? preview.variables : []
  return vars.filter(v => v && v.will_send && v.source_kind === 'secret')
}

function SendPreview({ preview, template }) {
  if (!preview) return null
  const vars = Array.isArray(preview.variables) ? preview.variables : []
  const labels = preview.labels ? Object.keys(preview.labels) : []
  const publish = Array.isArray(preview.publish) ? preview.publish : []
  const warnings = Array.isArray(preview.warnings) ? preview.warnings : []
  const row = (name, value) =>
    h('div', { key: name, className: cx('flex gap-2', SMALL) }, h('span', { className: cx('w-24 shrink-0', MUTED) }, name), h('span', { className: 'min-w-0 break-words' }, value))
  return h(
    'div',
    { className: 'flex flex-col gap-2' },
    row('Image', preview.image),
    row('Command', (preview.command || []).join(' ')),
    row('Limits', fmtNum(preview.cpus) + ' CPUs \u00b7 ' + fmtNum(preview.memory_gb) + ' GB memory \u00b7 ' + (preview.gpus || 0) + ' GPUs'),
    row('Network', preview.network || 'default'),
    row('Screen', preview.display ? 'Yes' : 'No'),
    publish.length ? row('Ports', publish.join(', ')) : null,
    labels.length ? row('Labels', labels.map(k => k + '=' + preview.labels[k]).join(', ')) : null,
    h(
      'table',
      { className: cx('w-full', SMALL) },
      h(
        'thead',
        {},
        h(
          'tr',
          { className: cx('text-left', MUTED) },
          ['Variable', 'What it is', 'Kind', 'Scope', 'Sent', 'Why'].map(c => h('th', { key: c, className: 'py-0.5 pr-3 font-medium' }, c))
        )
      ),
      h(
        'tbody',
        {},
        vars.length
          ? vars.map(v =>
              h(
                'tr',
                { key: v.name },
                h('td', { className: 'py-0.5 pr-3 font-mono' }, v.name),
                h('td', { className: 'py-0.5 pr-3' }, variableDescription(v, template) || 'No description'),
                h('td', { className: 'py-0.5 pr-3' }, v.source_kind),
                h('td', { className: 'py-0.5 pr-3' }, v.scope),
                h('td', { className: 'py-0.5 pr-3' }, v.will_send ? 'Yes' : 'No'),
                h('td', { className: cx('py-0.5', MUTED) }, v.reason || '')
              )
            )
          : h('tr', {}, h('td', { colSpan: 6, className: MUTED }, 'No variables.'))
      )
    ),
    warnings.length ? h('ul', { className: cx('flex flex-col gap-0.5 text-destructive', SMALL) }, warnings.map((w, i) => h('li', { key: i }, w))) : null
  )
}

function PreviewPanel({ template, hosts, onClose }) {
  const key = useQueryKey()
  const eligible = eligibleHosts(template, hosts)
  const [hostId, setHostId] = useState(eligible[0] ? eligible[0].spec.id : '')
  const q = useQuery({
    queryKey: key('preview', template.id, hostId),
    queryFn: () => api('GET', '/templates/' + encodeURIComponent(template.id) + '/preview?host=' + encodeURIComponent(hostId)),
    enabled: !!hostId,
    retry: false
  })
  return h(
    'div',
    { className: CARD },
    h(
      'div',
      { className: 'flex items-center justify-between gap-2' },
      h(Tip, { label: COPY.tooltips.send_preview }, h('span', { className: 'text-sm font-semibold' }, 'What will be sent: ' + template.name)),
      h(Button, { size: 'xs', variant: 'ghost', onClick: onClose }, 'Close')
    ),
    h('div', { className: cx(SMALL, MUTED) }, COPY.tooltips.send_preview),
    eligible.length
      ? h(Field, { label: 'On host' }, h(Pick, { value: hostId, onChange: setHostId, options: eligible.map(i => ({ value: i.spec.id, label: i.spec.name || i.spec.id })) }))
      : h('div', { className: cx(SMALL, MUTED) }, 'No host of a matching type is set up yet.'),
    hostId ? h(Async, { q, label: 'Building the preview\u2026' }, data => h(SendPreview, { preview: data, template })) : null
  )
}

// Row 20a: nothing is sent into an environment without the user seeing it. The list comes
// from the preview route; when any secret would be sent, Spawn stays off until it is ticked.
function SpawnDialog({ templates, hosts, template, hostId, onClose, onSpawned }) {
  const key = useQueryKey()
  const spawnable = (templates || []).filter(t => !isAgentTemplate(t))
  const [tid, setTid] = useState(template ? template.id : '')
  const [hid, setHid] = useState(hostId || '')
  const [agreed, setAgreed] = useState('')
  const act = useAction()
  const chosen = template || spawnable.find(t => t.id === tid) || null
  const eligible = chosen ? eligibleHosts(chosen, hosts) : []
  const chosenHost = eligible.find(i => i.spec.id === hid) || null
  const machineNote = spawnMachineNote(chosenHost)
  const previewQ = useQuery({
    queryKey: key('preview', chosen ? chosen.id : '', hid),
    queryFn: () => api('GET', '/templates/' + encodeURIComponent(chosen.id) + '/preview?host=' + encodeURIComponent(hid)),
    enabled: !!chosen && !!hid,
    retry: false
  })
  const preview = previewQ.data
  const vars = preview && Array.isArray(preview.variables) ? preview.variables : []
  const secrets = secretsToSend(preview)
  // The tick belongs to one list of secrets on one host; any change asks again.
  const consentKey = (chosen ? chosen.id : '') + '|' + hid + '|' + secrets.map(v => v.name).join(',')
  const consented = secrets.length === 0 || agreed === consentKey
  const previewFailed = !!hid && !!chosen && (previewQ.isError || previewQ.status === 'error')
  const ready = !!chosen && !!hid && preview !== undefined && !previewFailed
  const spawn = () =>
    act.run(async () => {
      const inst = await api('POST', '/instances', { template_id: chosen.id, host_id: hid })
      const heads = preview && Array.isArray(preview.warnings) ? preview.warnings.filter(w => typeof w === 'string' && w) : []
      toast('success', 'Starting ' + ((inst && inst.name) || chosen.name) + '.')
      if (heads.length) toast('warning', 'Heads-up for ' + ((inst && inst.name) || chosen.name) + ': ' + heads.join(' '))
      invalidateAll()
      onSpawned(inst)
    })
  const sentLine = v =>
    h(
      'li',
      { key: v.name, className: cx('flex flex-col gap-0.5 rounded-md border border-(--ui-stroke-secondary) px-2.5 py-1.5', SMALL) },
      h('span', { className: 'flex flex-wrap items-center gap-2' }, h('code', { className: 'font-semibold' }, v.name), v.source_kind === 'secret' ? h(Badge, { variant: 'warn' }, 'secret') : null, v.will_send ? null : h(Badge, { variant: 'muted' }, /not set/.test(v.reason || '') ? 'not set, will not be sent' : 'will not be sent')),
      h('span', { className: SECONDARY }, variableDescription(v, chosen) || 'No description.'),
      !v.will_send && v.reason && !/not set/.test(v.reason) ? h('span', { className: MUTED }, v.reason) : null
    )
  return h(
    Modal,
    { open: true, onClose, title: 'Spawn environment', description: 'Pick a template and a host to start it on.', className: 'max-h-[85vh] overflow-y-auto' },
    h(
      'div',
      { className: 'flex flex-col gap-3' },
      template
        ? h('div', { className: cx(SMALL, SECONDARY) }, 'Template: ' + template.name)
        : h(Field, { label: 'Template' }, h(Pick, { value: tid, onChange: v => { setTid(v); setHid('') }, options: spawnable.map(t => ({ value: t.id, label: t.name || t.id })) })),
      chosen
        ? eligible.length
          ? h(Field, { label: 'Host' }, h(Pick, { value: hid, onChange: setHid, options: eligible.map(i => ({ value: i.spec.id, label: hostChoiceLabel(i) })) }))
          : h('div', { className: cx(SMALL, MUTED) }, 'No host of a matching type is set up. Add one on the Hosts tab.')
        : null,
      machineNote.text ? h('div', { className: cx(SMALL, machineNote.blocked ? 'text-destructive' : MUTED) }, machineNote.blocked ? 'This host cannot be used right now: ' + machineNote.text + '.' : 'This host has no running machine: spawning ' + machineNote.text + '.') : null,
      chosen && hid
        ? previewFailed
          ? h(ErrorLine, { message: "Couldn't check what will be sent: " + errInfo(previewQ.error).message + ' Spawn is off until this works.' })
          : preview === undefined
            ? h(Loading, { label: 'Checking what will be sent\u2026' })
            : h(
                'div',
                { className: 'flex flex-col gap-2' },
                h('div', { className: cx('font-medium', SMALL, SECONDARY) }, vars.length ? 'Variables for this environment' : 'No variables are sent into this environment.'),
                vars.length ? h('ul', { className: 'flex flex-col gap-1.5' }, vars.map(sentLine)) : null,
                Array.isArray(preview.warnings) && preview.warnings.length
                  ? h('ul', { className: cx('flex flex-col gap-0.5', SMALL) }, preview.warnings.map((w, i) => h('li', { key: i, className: 'text-destructive' }, 'Heads-up: ' + w)))
                  : null,
                secrets.length
                  ? h(
                      'label',
                      { className: cx('flex items-start gap-2', SMALL) },
                      h(Checkbox, { checked: consented, onCheckedChange: v => setAgreed(v === true ? consentKey : ''), disabled: act.busy }),
                      h('span', { className: 'flex flex-col' }, h('span', { className: cx('font-medium', SECONDARY) }, 'Send these secrets into the environment'), h('span', { className: MUTED }, secrets.map(v => v.name).join(', ') + ' will be available to everything that runs in it.'))
                    )
                  : null
              )
        : null,
      h(ErrorLine, { message: act.error }),
      h(
        'div',
        { className: 'flex justify-end gap-2' },
        h(Button, { variant: 'ghost', onClick: onClose }, 'Cancel'),
        h(Button, { disabled: act.busy || !ready || !consented || machineNote.blocked, onClick: spawn }, act.busy ? 'Starting\u2026' : 'Spawn')
      )
    )
  )
}

function AgentSection({ agent, model, baseUrl, onModel, onBaseUrl }) {
  const mode = agent.mode || 'hermes'
  return h(
    Section,
    { title: 'Agent' },
    h('div', { className: cx(SMALL, SECONDARY) }, 'Mode: ' + mode),
    mode === 'script'
      ? h('div', { className: cx(SMALL, MUTED) }, 'A scripted agent uses no model and sends no secrets.')
      : h(
          Fragment,
          {},
          h(
            'div',
            { className: 'grid gap-2 sm:grid-cols-2' },
            h(TextField, { label: 'Model', value: model, onChange: onModel, placeholder: 'Default from Settings', hint: 'Blank uses the default from Settings.' }),
            h(TextField, { label: 'Model base URL', value: baseUrl, onChange: onBaseUrl, placeholder: 'Default from Settings', hint: 'Blank uses the default from Settings.' })
          ),
          h('div', { className: cx(SMALL, MUTED) }, 'Model key: ' + (agent.key_secret || AGENT_KEY_ENV) + ' (set in your Hermes profile .env)')
        )
  )
}

function TemplateEditor({ template, builtin, onClose }) {
  const [name, setName] = useState(template.name || '')
  const [display, setDisplay] = useState(!!template.display)
  const [network, setNetwork] = useState(template.network || 'default')
  const [ttl, setTtl] = useState(template.ttl_minutes == null ? '' : String(template.ttl_minutes))
  const [idle, setIdle] = useState(template.idle_minutes == null ? '' : String(template.idle_minutes))
  const [cpus, setCpus] = useState(String(template.cpus == null ? 2 : template.cpus))
  const [mem, setMem] = useState(String(template.memory_gb == null ? 4 : template.memory_gb))
  const [gpus, setGpus] = useState(String(template.gpus || 0))
  const [portsText, setPortsText] = useState(Array.isArray(template.ports) ? template.ports.join(', ') : '')
  const [vars, setVars] = useState(() => (template.variables || []).map(v => ({ name: v.name || '', source: v.source || '', scope: v.scope || 'redacted', hosts: v.hosts == null ? null : v.hosts, description: v.description || '' })))
  // Cloud-agent templates carry an `agent` object. Blank model / base URL mean
  // "use the defaults from Settings"; the model key itself never shows here.
  const agent = template.agent && typeof template.agent === 'object' ? template.agent : null
  const [agentModel, setAgentModel] = useState(agent && agent.model ? String(agent.model) : '')
  const [agentUrl, setAgentUrl] = useState(agent && agent.base_url ? String(agent.base_url) : '')
  const act = useAction()
  const isJob = template.kind === 'slurm_job'     // batch jobs never take the Settings defaults
  const isBuiltin = builtin === undefined ? !!template.builtin : !!builtin

  const setVar = (i, next) => setVars(list => list.map((v, j) => (j === i ? Object.assign({}, v, next) : v)))
  const save = () =>
    act.run(async () => {
      const trimmed = name.trim()
      if (!trimmed) throw new Error('A name is required.')
      const checked = validatePorts(portsText, display)
      if (checked.error) throw new Error(checked.error)
      const body = {
        ports: checked.ports,
        name: trimmed,
        display,
        network,
        ttl_minutes: numOrNull(ttl),
        idle_minutes: numOrNull(idle),
        cpus: numOrNull(cpus) == null ? template.cpus : numOrNull(cpus),
        memory_gb: numOrNull(mem) == null ? template.memory_gb : numOrNull(mem),
        gpus: Math.max(0, Math.round(numOrNull(gpus) || 0)),
        variables: vars.filter(v => v.name.trim()).map(v => ({ name: v.name.trim(), source: v.source.trim(), scope: v.scope, hosts: v.hosts, description: v.description.trim() }))
      }
      // The route replaces `agent` as a whole, so send the full object back.
      if (agent) body.agent = Object.assign({}, agent, { model: nz(agentModel), base_url: nz(agentUrl) })
      await api('PATCH', '/templates/' + encodeURIComponent(template.id), body)
      toast('success', 'Saved ' + trimmed + '.')
      invalidateAll()
      onClose()
    })

  return h(
    Modal,
    { open: true, onClose, title: 'Edit ' + template.name, className: 'max-h-[85vh] overflow-y-auto' },
    h(
      'div',
      { className: 'flex flex-col gap-3' },
      h(TextField, { label: 'Name', value: name, onChange: setName, hint: isBuiltin ? 'Changes are saved as your version of this built-in. Reset to default restores the original.' : 'Shown in lists and in the Spawn dialog.' }),
      h(ToggleRow, { label: 'Show a screen', hint: 'Adds a remote desktop to the environment. Needs an image with a screen server, such as the Desktop (noVNC) template.', checked: display, onChange: setDisplay }),
      h(TextField, { label: 'Published ports', value: portsText, onChange: setPortsText, placeholder: '8000, 3000', hint: 'Container ports to make reachable from this computer, separated by commas (1 to 65535, at most ' + MAX_PUBLISHED_PORTS + (display ? ', not ' + DISPLAY_PORT + ', which the screen uses' : '') + '). Each gets a free port on the host; use Forward on the environment to reach it.' }),
      h(Field, { label: 'Network' }, h(SegmentedControl, { value: network, onChange: setNetwork, options: [{ id: 'default', label: 'Default' }, { id: 'none', label: 'None' }] })),
      h(
        'div',
        { className: 'grid gap-2 sm:grid-cols-3' },
        h(TextField, { label: 'CPUs', value: cpus, onChange: setCpus, type: 'number' }),
        h(TextField, { label: 'Memory (GB)', value: mem, onChange: setMem, type: 'number' }),
        h(TextField, { label: 'GPUs', value: gpus, onChange: setGpus, type: 'number' }),
        h(TextField, { label: isJob ? 'Time limit (minutes, blank = none)' : 'Time limit (minutes, blank = Settings default)', value: ttl, onChange: setTtl, type: 'number' }),
        h(Tip, { label: COPY.tooltips.idle_timeout }, h('div', {}, h(TextField, { label: isJob ? 'Idle timeout (minutes, blank = none)' : 'Idle timeout (minutes, blank = Settings default)', value: idle, onChange: setIdle, type: 'number', hint: COPY.tooltips.idle_timeout })))
      ),
      h(
        Section,
        {
          title: 'Variables',
          actions: h(Button, { size: 'xs', variant: 'outline', onClick: () => setVars(list => list.concat([{ name: '', source: 'secret:', scope: 'redacted', hosts: null, description: '' }])) }, 'Add variable')
        },
        vars.length
          ? vars.map((v, i) =>
              h(
                'div',
                { key: i, className: 'flex flex-col gap-1.5 rounded-md border border-(--ui-stroke-secondary) p-2' },
                h(
                  'div',
                  { className: 'grid items-end gap-2 sm:grid-cols-[1fr_1.4fr_9rem_auto]' },
                  h(TextField, { label: 'Variable name', value: v.name, onChange: val => setVar(i, { name: val }) }),
                  h(TextField, { label: 'Source', value: v.source, onChange: val => setVar(i, { source: val }), placeholder: 'secret:NAME or literal:value' }),
                  h(Field, { label: 'Scope' }, h(Pick, { value: v.scope, onChange: val => setVar(i, { scope: val }), options: [{ value: 'runtime', label: 'runtime' }, { value: 'redacted', label: 'redacted' }, { value: 'agent-visible', label: 'agent-visible' }] })),
                  h(Button, { size: 'xs', variant: 'ghost', onClick: () => setVars(list => list.filter((_, j) => j !== i)) }, 'Remove')
                ),
                h(TextField, { label: 'Description', value: v.description, onChange: val => setVar(i, { description: val }), placeholder: 'What it is and why the environment needs it' }),
                h(
                  'div',
                  { className: cx('flex flex-wrap items-center gap-2', SMALL, MUTED) },
                  String(v.source).indexOf('secret:') === 0 ? h(Badge, { variant: 'warn' }, 'secret') : null,
                  h('span', {}, 'Sent into the environment when it starts')
                )
              )
            )
          : h('div', { className: cx(SMALL, MUTED) }, 'No variables.')
      ),
      agent ? h(AgentSection, { agent, model: agentModel, baseUrl: agentUrl, onModel: setAgentModel, onBaseUrl: setAgentUrl }) : null,
      h(ErrorLine, { message: act.error }),
      h('div', { className: 'flex justify-end gap-2' }, h(Button, { variant: 'ghost', onClick: onClose }, 'Cancel'), h(Button, { disabled: act.busy, onClick: save }, act.busy ? 'Saving\u2026' : 'Save'))
    )
  )
}

function NewTemplateDialog({ templates, onClose, onCreated }) {
  const [name, setName] = useState('')
  const [image, setImage] = useState('')
  const [from, setFrom] = useState(NO_BASE)
  const act = useAction()
  const source = from === NO_BASE ? null : (templates || []).find(t => t.id === from) || null
  const pickBase = v => {
    setFrom(v)
    const base = (templates || []).find(t => t.id === v)
    if (base && !image.trim()) setImage(base.image || '')
  }
  const create = () =>
    act.run(async () => {
      if (!name.trim()) throw new Error('Give the template a name.')
      if (!image.trim()) throw new Error('Give the template an image, for example docker.io/library/python:3.12-slim.')
      const body = source ? Object.assign({}, source, { name: name.trim(), image: image.trim() }) : { name: name.trim(), image: image.trim() }
      if (source) {
        delete body.id
        delete body.builtin
      }
      const made = await api('POST', '/templates', body)
      toast('success', 'Created ' + ((made && made.name) || name.trim()) + '.')
      invalidateAll()
      onCreated(made)
    })
  return h(
    Modal,
    { open: true, onClose, title: 'New template', description: 'A template is a reusable recipe for an environment. Edit its limits and variables afterwards.' },
    h(
      'div',
      { className: 'flex flex-col gap-3' },
      h(TextField, { label: 'Name', value: name, onChange: setName, placeholder: 'My lab environment' }),
      h(TextField, { label: 'Image', value: image, onChange: setImage, placeholder: 'docker.io/library/python:3.12-slim', hint: 'The container image the environment starts from.' }),
      h(
        Field,
        { label: 'Start from (optional)', hint: 'Copies the limits, variables and other settings of an existing template.' },
        h(Pick, { value: from, onChange: pickBase, options: [{ value: NO_BASE, label: 'Nothing: a blank template' }].concat((templates || []).map(t => ({ value: t.id, label: t.name || t.id }))) })
      ),
      h(ErrorLine, { message: act.error }),
      h('div', { className: 'flex justify-end gap-2' }, h(Button, { variant: 'ghost', onClick: onClose }, 'Cancel'), h(Button, { disabled: act.busy, onClick: create }, act.busy ? 'Creating\u2026' : 'Create'))
    )
  )
}

const NO_BASE = '__none__'

function TemplateCard({ template, info, onPreview, onSpawn, onEdit, onDuplicate, onDelete, onReset }) {
  const t = template
  const inf = info || { builtin: !!t.builtin, modified: false }
  const agentTpl = isAgentTemplate(t)
  const kinds = Array.isArray(t.host_kinds) ? t.host_kinds : []
  const facts = [
    fmtNum(t.cpus) + ' CPUs',
    fmtNum(t.memory_gb) + ' GB memory',
    (t.gpus || 0) + ' GPUs',
    t.ttl_minutes == null ? (t.kind === 'slurm_job' ? 'no time limit' : 'time limit from Settings') : 'time limit ' + t.ttl_minutes + ' min',
    t.idle_minutes == null ? (t.kind === 'slurm_job' ? 'no idle stop' : 'idle stop from Settings') : 'idle stop ' + t.idle_minutes + ' min',
    'network ' + (t.network || 'default')
  ].concat(Array.isArray(t.ports) && t.ports.length ? ['ports ' + t.ports.join(', ')] : [])
  return h(
    'div',
    { className: CARD },
    h(
      'div',
      { className: 'flex flex-wrap items-center justify-between gap-2' },
      h('span', { className: 'flex items-center gap-2' }, h('span', { className: 'text-sm font-semibold' }, t.name || t.id), inf.builtin ? h(Badge, { variant: 'outline' }, 'Built-in') : null, inf.modified ? h(Badge, { variant: 'warn' }, 'Modified') : null, t.display ? h(Badge, { variant: 'muted' }, 'screen') : null),
      h('span', { className: 'flex gap-1' }, kinds.map(k => h(Badge, { key: k, variant: 'outline' }, k)))
    ),
    t.description ? h('div', { className: cx(SMALL, SECONDARY) }, t.description) : null,
    h('div', { className: cx('font-mono', SMALL, MUTED) }, t.image),
    h('div', { className: cx('flex flex-wrap gap-x-3 gap-y-0.5', SMALL, SECONDARY) }, facts.map(f => h('span', { key: f }, f))),
    agentTpl ? h('div', { className: cx(SMALL, MUTED) }, 'Used by the Agents tab. Start a run there; it is not spawned from here.') : null,
    h(
      'div',
      { className: 'flex flex-wrap gap-1.5' },
      agentTpl ? null : h(Button, { size: 'xs', onClick: () => onSpawn(t) }, 'Spawn on\u2026'),
      h(Button, { size: 'xs', variant: 'outline', onClick: () => onPreview(t) }, 'What will be sent'),
      h(Button, { size: 'xs', variant: 'outline', onClick: () => onEdit(t) }, 'Edit'),
      h(Button, { size: 'xs', variant: 'outline', onClick: () => onDuplicate(t) }, 'Duplicate'),
      inf.modified ? h(Button, { size: 'xs', variant: 'outline', onClick: () => onReset(t) }, 'Reset to default') : null,
      !inf.builtin ? h(Button, { size: 'xs', variant: 'ghost', onClick: () => onDelete(t) }, 'Delete') : null
    )
  )
}

function TemplatesTab(props) {
  const p = props || {}
  const key = useQueryKey()
  const intent = useValue($intent)
  const [previewId, setPreviewId] = useState(null)
  const [editId, setEditId] = useState(null)
  const [spawn, setSpawn] = useState(null)
  const [deleting, setDeleting] = useState(null)
  const [resetting, setResetting] = useState(null)
  const [creating, setCreating] = useState(false)
  const act = useAction()
  const tplQ = useQuery({ queryKey: key('templates'), queryFn: () => api('GET', '/templates'), refetchInterval: POLL_MS, retry: 1 })
  const hostsQ = useQuery({ queryKey: key('hosts'), queryFn: () => api('GET', '/hosts'), refetchInterval: POLL_MS, retry: 1 })

  useEffect(() => {
    if (intent && intent.action === 'spawn') {
      setSpawn({ template: null, hostId: intent.hostId || '' })
      $intent.set(null)
    }
  }, [intent])

  const templates = Array.isArray(tplQ.data) ? tplQ.data : []
  const hosts = Array.isArray(hostsQ.data) ? hostsQ.data : []

  // The shipped version of every template that could be a built-in. A user template has
  // none (404), so an edited built-in is recognised by its id even though its flag is off.
  const candidates = templates.filter(t => t.builtin || SHIPPED_TEMPLATE_IDS.indexOf(t.id) >= 0).map(t => t.id)
  const defaultsQ = useQuery({
    queryKey: key('template-defaults', candidates.join(',')),
    queryFn: async () => {
      const out = {}
      await Promise.all(
        candidates.map(async id => {
          try {
            out[id] = await api('GET', '/templates/' + encodeURIComponent(id) + '?default=1')
          } catch (_) {
            out[id] = null
          }
        })
      )
      return out
    },
    enabled: candidates.length > 0,
    retry: false
  })
  const defaults = defaultsQ.data || {}
  const infoOf = t => templateInfo(t, defaults)

  const duplicate = t =>
    act.run(async () => {
      // No id: the backend makes a free one from the name.
      const body = Object.assign({}, t, { name: (t.name || t.id) + ' (copy)', builtin: false })
      delete body.id
      await api('POST', '/templates', body)
      toast('success', 'Created ' + body.name + '.')
      invalidateAll()
    })

  const previewTemplate = templates.find(t => t.id === previewId) || null
  const editTemplate = templates.find(t => t.id === editId) || null
  const envTemplates = templates.filter(t => !isAgentTemplate(t))
  const agentTemplates = templates.filter(isAgentTemplate)
  const card = t =>
    h(TemplateCard, {
      key: t.id,
      template: t,
      info: infoOf(t),
      onPreview: x => setPreviewId(x.id),
      onSpawn: x => setSpawn({ template: x, hostId: '' }),
      onEdit: x => setEditId(x.id),
      onDuplicate: duplicate,
      onDelete: x => setDeleting(x),
      onReset: x => setResetting(x)
    })

  return h(
    'div',
    { className: 'flex flex-col gap-3' },
    h(
      'div',
      { className: 'flex flex-wrap items-center justify-between gap-2' },
      h('div', { className: cx(SMALL, SECONDARY) }, 'A template describes an environment: image, limits, screen and variables.'),
      h(Button, { size: 'sm', onClick: () => setCreating(true) }, '+ New template')
    ),
    h(ErrorLine, { message: act.error }),
    previewTemplate ? h(PreviewPanel, { key: previewTemplate.id, template: previewTemplate, hosts, onClose: () => setPreviewId(null) }) : null,
    h(Async, { q: tplQ, label: 'Loading templates\u2026' }, () => {
      if (!templates.length) return h(EmptyState, { title: 'No templates', description: 'Templates come with the plugin; none were returned.' })
      return h(
        'div',
        { className: 'flex flex-col gap-4' },
        h(Section, { title: 'Environment templates' }, envTemplates.length ? h('div', { className: 'flex flex-col gap-3' }, envTemplates.map(card)) : h('div', { className: cx(SMALL, MUTED) }, 'No environment templates.')),
        h(
          Section,
          { title: 'Cloud agent templates' },
          h('div', { className: cx(SMALL, MUTED) }, 'The Agents tab uses these to run cloud agents. They are not spawned from here.'),
          agentTemplates.length ? h('div', { className: 'flex flex-col gap-3' }, agentTemplates.map(card)) : h('div', { className: cx(SMALL, MUTED) }, 'No cloud agent templates.')
        )
      )
    }),
    spawn
      ? h(SpawnDialog, {
          templates,
          hosts,
          template: spawn.template,
          hostId: spawn.hostId,
          onClose: () => setSpawn(null),
          onSpawned: () => {
            setSpawn(null)
            if (p.goTab) p.goTab('overview')
          }
        })
      : null,
    creating
      ? h(NewTemplateDialog, {
          templates,
          onClose: () => setCreating(false),
          onCreated: made => {
            setCreating(false)
            if (made && made.id) setEditId(made.id)
          }
        })
      : null,
    editTemplate ? h(TemplateEditor, { key: editTemplate.id, template: editTemplate, builtin: infoOf(editTemplate).builtin, onClose: () => setEditId(null) }) : null,
    deleting
      ? h(ConfirmDialog, {
          open: true,
          destructive: true,
          title: 'Delete ' + (deleting.name || deleting.id) + '?',
          description: 'Environments already started from it keep running.',
          confirmLabel: 'Delete',
          onClose: () => setDeleting(null),
          onConfirm: async () => {
            try {
              await api('DELETE', '/templates/' + encodeURIComponent(deleting.id))
            } catch (e) {
              throw new Error(errInfo(e).message)
            }
            invalidateAll()
          }
        })
      : null,
    resetting
      ? h(ConfirmDialog, {
          open: true,
          title: 'Reset ' + (resetting.name || resetting.id) + ' to default?',
          description: 'Your changes to this built-in are discarded and the original comes back. Environments already started from it keep running.',
          confirmLabel: 'Reset to default',
          onClose: () => setResetting(null),
          onConfirm: async () => {
            try {
              await api('DELETE', '/templates/' + encodeURIComponent(resetting.id))
            } catch (e) {
              throw new Error(errInfo(e).message)
            }
            toast('success', 'Reset ' + (resetting.name || resetting.id) + ' to its default.')
            invalidateAll()
          }
        })
      : null
  )
}

// ---------------------------------------------------------------------------
// Jobs tab
// ---------------------------------------------------------------------------

const TIME_LIMIT_RE = /^(\d+-)?\d{1,3}(:\d{1,2}){0,2}$/
const ARRAY_RE = /^\d+(-\d+)?(:\d+)?(,\d+(-\d+)?(:\d+)?)*(%\d+)?$/

// Select handlers get a plain value; accept a change event too.
function plain(v) {
  return v && typeof v === 'object' && v.target ? v.target.value : v
}

function text(set) {
  return v => set(plain(v))
}

function isSlurmItem(item) {
  return !!(item && item.spec && item.spec.kind === 'slurm')
}

function jobTone(state) {
  if (state === 'RUNNING' || state === 'COMPLETED') return 'good'
  if (state === 'PENDING' || state === 'COMPLETING') return 'warn'
  if (JOB_FAILED.indexOf(state) >= 0) return 'bad'
  return 'muted'
}

// Turns the dialog fields into a JobRequest, or an error message. Only
// JobRequest keys are sent; whichever of command/script is unused is null.
function buildJobRequest(f) {
  if (!f.host_id) return { error: 'Pick a Slurm host.' }
  const name = String(f.name || '').trim()
  if (!name) return { error: 'Give the job a name.' }
  if (name.length > 64) return { error: 'The name can be at most 64 characters.' }
  const command = f.mode === 'script' ? null : nz(f.command)
  const script = f.mode === 'script' ? (String(f.script || '').trim() ? String(f.script) : null) : null
  if (f.mode === 'script' ? !script : !command) {
    return { error: f.mode === 'script' ? 'Enter the script to run.' : 'Enter the command to run.' }
  }
  const image = nz(f.image)
  if (image && !/^docker:\/\//i.test(image) && !/\.sif$/i.test(image)) {
    return { error: 'The image must start with docker:// or be a path ending in .sif.' }
  }
  const cpus = Number(String(f.cpus).trim())
  if (!Number.isInteger(cpus) || cpus < 1) return { error: 'CPUs must be a whole number of at least 1.' }
  const mem = Number(String(f.mem_gb).trim())
  if (!Number.isFinite(mem) || mem <= 0) return { error: 'Memory must be more than 0 GB.' }
  const gpus = Number(String(f.gpus).trim() || '0')
  if (!Number.isInteger(gpus) || gpus < 0) return { error: 'GPUs must be a whole number, 0 or more.' }
  const time = nz(f.time_limit) || '00:30:00'
  if (!TIME_LIMIT_RE.test(time)) return { error: 'The time limit looks like 00:30:00 or 1-12:00:00.' }
  const array = nz(f.array)
  if (array && !ARRAY_RE.test(array)) return { error: 'The array looks like 1-10 or 1-10%2.' }
  return {
    body: {
      host_id: f.host_id,
      name,
      command,
      script,
      image,
      partition: nz(f.partition),
      cpus,
      mem_gb: mem,
      gpus,
      time_limit: time,
      array
    }
  }
}

function SubmitJobDialog(props) {
  const p = props || {}
  const key = useQueryKey()
  const hostsQ = useQuery({
    queryKey: key('hosts', 'slurm'),
    queryFn: () => api('GET', '/hosts?kind=slurm'),
    enabled: !Array.isArray(p.hosts),
    retry: 1
  })
  const items = (Array.isArray(p.hosts) ? p.hosts : Array.isArray(hostsQ.data) ? hostsQ.data : []).filter(isSlurmItem)
  const [hostChoice, setHostChoice] = useState(p.hostId || '')
  const [partChoice, setPartChoice] = useState('')
  const [name, setName] = useState('')
  const [mode, setMode] = useState('command')
  const [command, setCommand] = useState('')
  const [script, setScript] = useState('')
  const [image, setImage] = useState('')
  const [cpus, setCpus] = useState('1')
  const [mem, setMem] = useState('1')
  const [gpus, setGpus] = useState('0')
  const [time, setTime] = useState('00:30:00')
  const [array, setArray] = useState('')
  const [localError, setLocalError] = useState('')
  const act = useAction()

  const hostId = hostChoice || (items[0] ? items[0].spec.id : '')
  const hostItem = items.find(i => i.spec.id === hostId) || null
  const partsQ = useQuery({
    queryKey: key('partitions', hostId),
    queryFn: () => api('GET', '/hosts/' + encodeURIComponent(hostId) + '/partitions'),
    enabled: !!hostId,
    retry: false
  })
  const parts = Array.isArray(partsQ.data) ? partsQ.data : []
  const flagged = parts.find(x => x && x.default)
  const hostDefault = hostItem && hostItem.spec.slurm ? hostItem.spec.slurm.default_partition : null
  const defaultPartition = (flagged && flagged.name) || hostDefault || (parts[0] && parts[0].name) || ''
  const partition = partChoice || defaultPartition

  const close = () => {
    if (p.onClose) p.onClose()
  }

  const submit = () => {
    const built = buildJobRequest({ host_id: hostId, name, mode, command, script, image, partition, cpus, mem_gb: mem, gpus, time_limit: time, array })
    if (built.error) {
      setLocalError(built.error)
      return
    }
    setLocalError('')
    act.run(async () => {
      await api('POST', '/jobs', built.body)
      toast('success', 'Submitted job ' + built.body.name + '.')
      invalidateAll()
      close()
    })
  }

  if (p.open === false) return null

  const hostOptions = items.map(i => ({ value: i.spec.id, label: i.spec.name || i.spec.id }))
  const partOptions = parts.map(x => ({ value: x.name, label: x.name + (x.default ? ' (default)' : '') + (Number.isFinite(x.cpus_idle) ? ' \u00b7 ' + x.cpus_idle + ' CPUs idle' : '') }))

  return h(
    Modal,
    {
      open: true,
      onClose: close,
      title: 'Submit job',
      description: 'Runs through sbatch on the chosen Slurm host.',
      className: 'max-h-[85vh] overflow-y-auto'
    },
    h(
      'div',
      { className: 'flex flex-col gap-3' },
      items.length
        ? h(Field, { label: 'Host' }, h(Pick, { value: hostId, onChange: v => { setHostChoice(plain(v)); setPartChoice('') }, options: hostOptions }))
        : h('div', { className: cx(SMALL, MUTED) }, 'No Slurm host is set up. Add one on the Hosts tab.'),
      h(TextField, { label: 'Name', value: name, onChange: text(setName), placeholder: 'train-small' }),
      h(SegmentedControl, {
        value: mode,
        onChange: setMode,
        options: [
          { id: 'command', label: 'Command' },
          { id: 'script', label: 'Script' }
        ]
      }),
      mode === 'command'
        ? h(TextField, { label: 'Command', value: command, onChange: text(setCommand), placeholder: 'python train.py --epochs 3', hint: 'Runs in a login shell in the job directory.' })
        : h(
            Field,
            { label: 'Script', hint: 'A batch script body. #SBATCH lines for resources are added for you.' },
            h(Textarea, { value: script, rows: 8, className: 'font-mono', placeholder: '#!/bin/bash\npython train.py', onChange: ev => setScript(ev.target.value) })
          ),
      h(TextField, { label: 'Image (optional)', value: image, onChange: text(setImage), placeholder: 'docker://python:3.12-slim', hint: 'A docker:// reference or the path of a .sif file. Leave blank for a plain job.' }),
      parts.length
        ? h(Field, { label: 'Partition' }, h(Pick, { value: partition, onChange: v => setPartChoice(plain(v)), options: partOptions }))
        : h(TextField, { label: 'Partition (optional)', value: partition, onChange: text(setPartChoice) }),
      h(
        'div',
        { className: 'grid gap-2 sm:grid-cols-4' },
        h(TextField, { label: 'CPUs', value: cpus, onChange: text(setCpus), type: 'number' }),
        h(TextField, { label: 'Memory (GB)', value: mem, onChange: text(setMem), type: 'number' }),
        h(TextField, { label: 'GPUs', value: gpus, onChange: text(setGpus), type: 'number' }),
        h(TextField, { label: 'Time limit', value: time, onChange: text(setTime), placeholder: '00:30:00' })
      ),
      h(TextField, { label: 'Array (optional)', value: array, onChange: text(setArray), placeholder: '1-10%2', hint: 'Runs the job once per index. 1-10%2 runs ten tasks, two at a time.' }),
      h(ErrorLine, { message: localError || act.error }),
      h(
        'div',
        { className: 'flex justify-end gap-2' },
        h(Button, { variant: 'ghost', onClick: close }, 'Cancel'),
        h(Button, { disabled: act.busy || !items.length, onClick: submit }, act.busy ? 'Submitting\u2026' : 'Submit')
      )
    )
  )
}

function JobOutputDialog({ job, onClose }) {
  const key = useQueryKey()
  const [stream, setStream] = useState('out')
  const live = JOB_CANCELLABLE.indexOf(job.state) >= 0
  const q = useQuery({
    queryKey: key('job-output', job.id, stream),
    queryFn: () => api('GET', '/jobs/' + encodeURIComponent(job.id) + '/output?tail=200&stream=' + stream),
    refetchInterval: live ? POLL_MS : false,
    retry: 1
  })
  return h(
    Modal,
    {
      open: true,
      onClose,
      title: 'Output: ' + (job.name || job.id),
      footer: h(Button, { variant: 'outline', size: 'sm', onClick: () => invalidateAll() }, 'Refresh')
    },
    h(SegmentedControl, {
      value: stream,
      onChange: setStream,
      options: [
        { id: 'out', label: 'out' },
        { id: 'err', label: 'err' }
      ]
    }),
    h(Async, { q, label: 'Loading output\u2026' }, data => h(OutputBody, { data }))
  )
}

function JobCard({ job }) {
  const actions = jobActions(job)
  const act = useAction()
  const term = useTerminalCooldown()
  const [dialog, setDialog] = useState(null)
  const facts = [
    ['Host', job.host_id],
    ['Partition', job.partition],
    ['Nodes', job.nodes],
    ['Submitted', job.submitted_at ? ago(job.submitted_at) : null],
    ['Started', job.started_at ? ago(job.started_at) : null],
    ['Ended', job.ended_at ? ago(job.ended_at) : null],
    ['Exit code', job.exit_code]
  ].filter(f => f[1])

  const buttons = {
    output: h(Button, { key: 'output', size: 'xs', variant: 'outline', onClick: () => setDialog('output') }, 'Output'),
    cancel: h(Button, { key: 'cancel', size: 'xs', variant: 'ghost', onClick: () => setDialog('cancel') }, 'Cancel'),
    delete: h(Button, { key: 'delete', size: 'xs', variant: 'ghost', onClick: () => setDialog('delete') }, 'Remove from list'),
    terminal: h(
      Tip,
      { key: 'terminal', label: 'Opens a shell inside the running job.' },
      h(Button, { size: 'xs', variant: 'outline', disabled: act.busy || term.cooling, onClick: () => { if (term.cooling) return undefined; term.start(); return act.run(() => openTerminal({ kind: 'job', id: job.id, name: job.name || job.id })) } }, 'Terminal')
    )
  }

  return h(
    'div',
    { className: CARD },
    h(
      'div',
      { className: 'flex flex-wrap items-center justify-between gap-2' },
      h(
        'span',
        { className: 'flex min-w-0 items-center gap-2' },
        h(StatusDot, { tone: jobTone(job.state) }),
        h('span', { className: 'truncate text-sm font-semibold' }, job.name || job.id),
        h(Badge, { variant: JOB_FAILED.indexOf(job.state) >= 0 ? 'destructive' : 'muted' }, jobStateLabel(job)),
        job.stale ? h('span', { className: cx(SMALL, MUTED) }, 'not refreshed') : null
      ),
      h('span', { className: cx(SMALL, MUTED) }, job.slurm_job_id ? 'Slurm job ' + job.slurm_job_id : job.id)
    ),
    facts.length
      ? h('div', { className: cx('flex flex-wrap gap-x-4 gap-y-0.5', SMALL, SECONDARY) }, facts.map(f => h('span', { key: f[0] }, f[0] + ' ' + f[1])))
      : null,
    job.image ? h('div', { className: cx('font-mono', SMALL, MUTED) }, job.image) : null,
    h(ErrorLine, { message: act.error }),
    h('div', { className: 'flex flex-wrap gap-1.5' }, actions.map(a => buttons[a])),
    dialog === 'output' ? h(JobOutputDialog, { job, onClose: () => setDialog(null) }) : null,
    dialog === 'cancel'
      ? h(ConfirmDialog, {
          open: true,
          destructive: true,
          title: 'Cancel ' + (job.name || job.id) + '?',
          description: 'Slurm stops the job. Output written so far stays available.',
          confirmLabel: 'Cancel job',
          onClose: () => setDialog(null),
          onConfirm: async () => {
            try {
              await api('POST', '/jobs/' + encodeURIComponent(job.id) + '/cancel')
            } catch (e) {
              throw new Error(errInfo(e).message)
            }
            toast('success', 'Cancelled ' + (job.name || job.id) + '.')
            invalidateAll()
          }
        })
      : null,
    dialog === 'delete'
      ? h(ConfirmDialog, {
          open: true,
          destructive: true,
          title: 'Remove ' + (job.name || job.id) + ' from the list?',
          description: jobRemoveText(job),
          confirmLabel: 'Remove from list',
          onClose: () => setDialog(null),
          onConfirm: async () => {
            let res
            try {
              res = await api('DELETE', '/jobs/' + encodeURIComponent(job.id))
            } catch (e) {
              throw new Error(errInfo(e).message)
            }
            toast('success', (res && res.note) || 'Removed ' + (job.name || job.id) + ' from the list.')
            invalidateAll()
          }
        })
      : null
  )
}

// 'Clear finished': removes the records of every finished job on this computer, nothing on the cluster.
function ClearFinishedDialog({ count, onClose }) {
  return h(ConfirmDialog, {
    open: true,
    destructive: true,
    title: 'Remove ' + count + ' finished ' + plural(count, 'job', 'jobs') + ' from the list?',
    description: 'This only removes the records of the finished jobs from the list on this computer. Their job folders on the cluster stay, with their output files. Jobs that are still pending or running are not touched.',
    confirmLabel: 'Remove finished jobs',
    onClose,
    onConfirm: async () => {
      let res
      try {
        res = await api('POST', '/jobs/clear-finished', {})
      } catch (e) {
        throw new Error(errInfo(e).message)
      }
      toast('success', (res && res.note) || 'Removed the finished jobs from the list.')
      invalidateAll()
    }
  })
}

// Running and pending jobs of one partition on one host, counted from the jobs list.
// A job submitted without a partition ran in the host's default one.
function partitionLoad(jobs, hostId, part, hostDefault) {
  const mine = (Array.isArray(jobs) ? jobs : []).filter(j => {
    if (!j || j.host_id !== hostId) return false
    return j.partition ? j.partition === part.name : !!part.default || part.name === hostDefault
  })
  return {
    running: mine.filter(j => j.state === 'RUNNING' || j.state === 'COMPLETING').length,
    pending: mine.filter(j => j.state === 'PENDING').length
  }
}

function PartitionLoadChip({ load }) {
  if (!load || (!load.running && !load.pending)) return null
  return h(Badge, { variant: load.running ? 'success' : 'warn' }, load.running + ' running \u00b7 ' + load.pending + ' pending')
}

function PartitionsPanel({ items, jobs }) {
  return h(
    Section,
    { title: 'Partitions' },
    items.map(item => {
      const caps = (item.state && item.state.capabilities) || {}
      const parts = Array.isArray(caps.partitions) ? caps.partitions : []
      const nodes = Array.isArray(caps.nodes) ? caps.nodes : []
      const hostDefault = item.spec && item.spec.slurm ? item.spec.slurm.default_partition : null
      return h(
        'div',
        { key: item.spec.id, className: CARD },
        h('div', { className: 'flex items-center gap-2 text-sm font-semibold' }, h(LightDot, { status: item.state ? item.state.status : 'unknown' }), item.spec.name || item.spec.id),
        parts.length
          ? parts.map(pt => {
              const memUsed = Number.isFinite(pt.mem_total_mb) && Number.isFinite(pt.mem_free_mb) ? pt.mem_total_mb - pt.mem_free_mb : null
              return h(
                'div',
                { key: pt.name, className: 'flex flex-col gap-1' },
                h(
                  'div',
                  { className: cx('flex flex-wrap items-center gap-2', SMALL) },
                  h('span', { className: 'font-medium' }, pt.name),
                  pt.default ? h(Badge, { variant: 'outline' }, 'default') : null,
                  h(PartitionLoadChip, { load: partitionLoad(jobs, item.spec.id, pt, hostDefault) }),
                  h('span', { className: MUTED }, [pt.state, pt.nodes + ' ' + plural(pt.nodes, 'node', 'nodes'), pt.time_limit ? 'limit ' + pt.time_limit : null, pt.gres].filter(Boolean).join(' \u00b7 '))
                ),
                h(
                  'div',
                  { className: 'flex flex-wrap gap-3' },
                  h(Meter, { label: 'CPU ' + pt.cpus_idle + ' idle \u00b7 ' + pt.cpus_alloc + ' allocated', used: pt.cpus_alloc, total: pt.cpus_total, fmt: fmtNum }),
                  h(Meter, { label: 'Memory', used: memUsed, total: pt.mem_total_mb, fmt: fmtMb })
                )
              )
            })
          : h('div', { className: cx(SMALL, MUTED) }, 'No partition information yet. Probe the host from the Hosts tab.'),
        nodes.length
          ? h(
              'ul',
              { className: cx('flex flex-col gap-0.5', SMALL, SECONDARY) },
              nodes.map(n =>
                h(
                  'li',
                  { key: n.name },
                  [n.name, n.state, Number.isFinite(n.cpus_idle) ? n.cpus_idle + '/' + n.cpus_total + ' CPUs idle' : null, Number.isFinite(n.mem_free_mb) ? fmtMb(n.mem_free_mb) + ' memory free' : null].filter(Boolean).join(' \u00b7 ')
                )
              )
            )
          : null
      )
    })
  )
}

function JobsTab() {
  const key = useQueryKey()
  const intent = useValue($intent)
  const [submitting, setSubmitting] = useState(false)
  const [adding, setAdding] = useState(false)
  const [freshHost, setFreshHost] = useState('')
  const [clearing, setClearing] = useState(false)
  const jobsQ = useQuery({ queryKey: key('jobs'), queryFn: () => api('GET', '/jobs'), refetchInterval: POLL_MS, retry: 1 })
  const hostsQ = useQuery({ queryKey: key('hosts', 'slurm'), queryFn: () => api('GET', '/hosts?kind=slurm'), refetchInterval: POLL_MS, retry: 1 })
  const slurmHosts = (Array.isArray(hostsQ.data) ? hostsQ.data : []).filter(isSlurmItem)

  // The palette's "Submit Slurm job" waits for the host list, then opens the dialog.
  useEffect(() => {
    if (intent && intent.action === 'submit-job' && intent.fresh) {
      // Straight after the wizard: the host list is stale, so the dialog loads its own.
      $intent.set(null)
      setFreshHost(intent.hostId || '*')
      setSubmitting(true)
    } else if (intent && intent.action === 'submit-job' && hostsQ.data !== undefined) {
      $intent.set(null)
      if (slurmHosts.length) setSubmitting(true)
    }
  }, [intent, hostsQ.data])

  const addHost = h(
    Modal,
    { open: adding, onClose: () => setAdding(false), title: 'Add host', description: 'Connect a machine in three steps.' },
    adding
      ? h(AddHostWizard, {
          initial: { kind: 'slurm' },
          onCancel: () => setAdding(false),
          onFinished: () => setAdding(false)
        })
      : null
  )

  return h(
    'div',
    { className: 'flex flex-col gap-3' },
    h(Async, { q: hostsQ, label: 'Loading hosts\u2026' }, () => {
      if (!slurmHosts.length) {
        return h(
          'div',
          { className: 'flex flex-col items-center gap-2' },
          h(EmptyState, { title: 'No Slurm hosts yet', description: 'Add a Slurm login node to submit batch jobs and watch them here.' }),
          h(Button, { size: 'sm', onClick: () => setAdding(true) }, '+ Add host')
        )
      }
      return h(
        'div',
        { className: 'flex flex-col gap-4' },
        h(
          'div',
          { className: 'flex items-center justify-between gap-2' },
          h('div', { className: cx(SMALL, SECONDARY) }, 'Batch jobs on your Slurm hosts.'),
          h(Button, { size: 'sm', onClick: () => setSubmitting(true) }, 'Submit job\u2026')
        ),
        h(
          Section,
          {
            title: 'Jobs',
            actions: (Array.isArray(jobsQ.data) ? jobsQ.data : []).some(jobFinished)
              ? h(Tip, { label: 'Removes finished jobs from this list only. Their folders on the cluster stay.' }, h(Button, { size: 'xs', variant: 'outline', onClick: () => setClearing(true) }, 'Clear finished\u2026'))
              : null
          },
          h(Async, { q: jobsQ, label: 'Loading jobs\u2026' }, data => {
            const list = Array.isArray(data) ? data : []
            if (!list.length) return h(EmptyState, { title: 'No jobs yet', description: 'Submit a job to see it here.' })
            return h('div', { className: 'flex flex-col gap-3' }, list.map(job => h(JobCard, { key: job.id, job })))
          })
        ),
        h(PartitionsPanel, { items: slurmHosts, jobs: jobsQ.data })
      )
    }),
    submitting
      ? h(SubmitJobDialog, {
          hosts: freshHost ? undefined : slurmHosts,
          hostId: freshHost && freshHost !== '*' ? freshHost : undefined,
          onClose: () => {
            setSubmitting(false)
            setFreshHost('')
          }
        })
      : null,
    clearing
      ? h(ClearFinishedDialog, { count: (Array.isArray(jobsQ.data) ? jobsQ.data : []).filter(jobFinished).length, onClose: () => setClearing(false) })
      : null,
    addHost
  )
}

// ---------------------------------------------------------------------------
// Agents tab
// ---------------------------------------------------------------------------

const AGENT_ACTIVE = ['starting', 'running']
const AGENT_COLLECTABLE = ['done', 'failed', 'collected']
const AGENT_TEMPLATE_FALLBACK = [
  { id: 'hermes-agent', name: 'Hermes cloud agent', agent: { mode: 'hermes' } },
  { id: 'agent-script', name: 'Scripted cloud agent', agent: { mode: 'script' } }
]
const AGENT_BLURB =
  'A cloud agent is a Hermes agent that works on a branch of one of your git repositories inside a sandbox on one of your hosts, then hands the branch back to you.'

// Is the run's sandbox gone? A run that never got one (no instance id) or whose
// sandbox is missing from the instance list (destroyed by Stop or the reaper) has
// nothing left to collect from or stop. `instances` is the list from GET /instances;
// when it is not known yet (undefined) only the run itself is judged. A stopped
// sandbox still exists: Collect starts it again.
function agentSandboxGone(run, instances) {
  if (!run || !run.instance_id) return true
  if (!Array.isArray(instances)) return false
  return !instances.some(i => i && i.id === run.instance_id)
}

// The head the user already fetched, by run id: the Agents tab notes it when a run
// reaches 'collected', so later commits show up as new results. Kept in
// ctx.storage so a restart does not forget it.
const FETCHED_KEY = 'agent-fetched'
let agentFetched = null

function fetchedHeads() {
  if (agentFetched === null) {
    const saved = ctx && ctx.storage ? ctx.storage.get(FETCHED_KEY, null) : null
    agentFetched = saved && typeof saved === 'object' ? saved : {}
  }
  return agentFetched
}

function rememberFetched(runId, head) {
  if (!runId || !head) return
  const all = fetchedHeads()
  if (all[runId] === head) return
  all[runId] = head
  if (ctx && ctx.storage) ctx.storage.set(FETCHED_KEY, all)
}

// Where a run's results stand: 'none' (never fetched), 'fetched' (the branch and
// worktree are here and nothing newer exists) or 'new' (the agent committed again
// after the last fetch).
function agentResults(run, seen) {
  if (!run || !run.collected_ref) return 'none'
  if (run.status === 'collected') return 'fetched'
  const known = run.collected_head_sha || (seen && seen[run.id]) || null
  return known && run.head_sha && known === run.head_sha ? 'fetched' : 'new'
}

// A live run can always be stopped. Once it ended, 'Get results' (and Stop) need its
// sandbox; results that are already fetched with nothing new offer no button.
function agentActions(run, instances, seen) {
  const status = run && run.status
  if (AGENT_ACTIVE.indexOf(status) >= 0) return ['stop']
  if (agentSandboxGone(run, instances)) return []
  // A stopped run normally has no sandbox. When Stop kept it (its files could not be saved,
  // or its commits could not be fetched) it still exists: fetching and stopping work again.
  if (status === 'stopped' && !Array.isArray(instances)) return []
  const out = []
  const commits = (run.commits_ahead || 0) > 0
  const fetchable = status === 'stopped' ? commits : status === 'failed' ? commits : AGENT_COLLECTABLE.indexOf(status) >= 0
  // A run that failed before the agent did any work has nothing to fetch.
  if (fetchable && agentResults(run, seen) !== 'fetched') out.push('collect')
  out.push('stop')
  return out
}

// Running is green, starting amber, failed red; stopped, done and collected are neutral.
function agentTone(status) {
  if (status === 'running') return 'good'
  if (status === 'starting') return 'warn'
  if (status === 'failed') return 'bad'
  return 'muted'
}

function agentBadge(status) {
  if (status === 'running') return 'success'
  if (status === 'starting') return 'warn'
  if (status === 'failed') return 'destructive'
  return 'muted'
}

const HANDOFF_NAME = 'Handoff test (no model)'
const HANDOFF_HELP = 'Commits a marker file to prove the branch round trip. Does not use a model.'

function agentIsScript(t) {
  return !!(t && t.agent && t.agent.mode === 'script')
}

function agentTemplateLabel(t) {
  return agentIsScript(t) ? HANDOFF_NAME : (t && (t.name || t.id)) || ''
}

// ---- the New agent run draft ------------------------------------------------
// Everything typed into the dialog lives in this module, not in the dialog: it
// survives closing the dialog, switching tabs, leaving the page and a restart
// (ctx.storage). Only a successful start or 'Discard draft' clears it.

const DRAFT_KEY = 'agent-draft'
const DRAFT_DEFAULTS = { goal: '', criteria: '', host: '', repo: '', mode: 'new', branch: '', base: 'HEAD', template: '', turns: '30', origin: '', model: '', ep_name: '', ep_url: '', ep_model: '', ep_key: '' }
const $agentDraft = atom(null)
let draftLoaded = false

function draftHasContent(d) {
  return !!d && ['goal', 'criteria', 'repo', 'branch', 'ep_url', 'ep_model'].some(k => typeof d[k] === 'string' && d[k].trim() !== '')
}

function loadAgentDraft() {
  if (draftLoaded) return
  draftLoaded = true
  const saved = ctx && ctx.storage ? ctx.storage.get(DRAFT_KEY, null) : null
  if (saved && typeof saved === 'object' && draftHasContent(saved)) $agentDraft.set(Object.assign({}, DRAFT_DEFAULTS, saved))
}

function saveAgentDraft(next) {
  const value = Object.assign({}, DRAFT_DEFAULTS, next)
  $agentDraft.set(value)
  if (ctx && ctx.storage) ctx.storage.set(DRAFT_KEY, value)
}

function clearAgentDraft() {
  $agentDraft.set(null)
  if (ctx && ctx.storage) ctx.storage.remove(DRAFT_KEY)
}

// New agent run filled in from an earlier run, so a failed one can be corrected and
// started again. Fields the run record does not keep (base, turn limit) fall back to
// the dialog's defaults.
function draftFromRun(run) {
  const r = run || {}
  return Object.assign({}, DRAFT_DEFAULTS, {
    goal: r.goal || '',
    criteria: r.criteria || '',
    host: r.host_id || '',
    repo: r.repo_path || '',
    mode: r.new_branch === false ? 'existing' : 'new',
    branch: r.branch || '',
    base: r.base || DRAFT_DEFAULTS.base,
    template: r.template_id || '',
    turns: Number.isFinite(r.max_turns) ? String(r.max_turns) : DRAFT_DEFAULTS.turns,
    model: r.model_id ? 'm:' + r.model_id : '',
    origin: r.id || ''
  })
}

// What the backend sends back when the model is not set up, or the host forbids agents.
function agentFix(code, message) {
  const m = String(message || '')
  if (/agent\.(base_url|model)|model endpoint|model key|key secret|CC_AGENT_API_KEY|Settings \u2192 Own Compute/i.test(m)) return 'model'
  if (code === 'limit_reached' || /allow agents/i.test(m)) return 'host'
  return null
}

function AgentError({ message, code }) {
  if (!message) return null
  const fix = agentFix(code, message)
  return h(
    'div',
    { className: 'flex flex-col gap-1.5' },
    h(ErrorLine, { message }),
    fix === 'model'
      ? h(
          'div',
          { className: cx('flex flex-wrap items-center gap-2', SMALL, SECONDARY) },
          h(Button, { size: 'xs', variant: 'outline', onClick: () => host.navigate(pluginSettingsHref(PLUGIN_ID)) }, 'Open settings'),
          h(Button, { size: 'xs', variant: 'outline', onClick: () => setIntent('templates') }, 'Edit templates')
        )
      : null,
    fix === 'host'
      ? h(
          'div',
          { className: cx('flex flex-wrap items-center gap-2', SMALL, SECONDARY) },
          'Turn on "Allow cloud agents" in the host settings (Hosts tab, Edit, Sandbox defaults).',
          h(Button, { size: 'xs', variant: 'outline', onClick: () => setIntent('hosts') }, 'Open Hosts')
        )
      : null
  )
}

// ---- model choice in the run dialog -------------------------------------------
// One list: the default, the saved choices, what this Hermes profile offers (detected),
// entries the backend marks unavailable (greyed, with the reason) and 'Another endpoint'.
// Picking a detected entry or typing an endpoint saves it first, so a run only ever
// names a saved model id.

function hostOfUrl(url) {
  const m = /^https?:\/\/([^/\s:?#]+)/i.exec(String(url || '').trim())
  return m ? m[1] : ''
}

function modelData(data) {
  const d = data && typeof data === 'object' ? data : {}
  const list = k => (Array.isArray(d[k]) ? d[k].filter(x => x && typeof x === 'object') : [])
  return { saved: list('models'), detected: list('detected'), unavailable: list('unavailable'), defaultId: d.default_id || null }
}

function modelTitle(m) {
  return (m && (m.name || m.model || m.id)) || 'Model'
}

// What a rerun sends for 'Default model' when no saved choice is the default.
const DEFAULT_MODEL_MARKER = 'default'

// The saved choice that is the default, if any.
function defaultModel(md) {
  return md.saved.find(m => m.id === md.defaultId || m.is_default) || null
}

// Is the picked row the default model? The default's own saved row is not listed again, so
// picking it by id means the same thing.
function modelIsDefault(sel, md) {
  if (!sel) return true
  const d = defaultModel(md)
  return !!d && sel === 'm:' + d.id
}

// The model a run will use, in words, for the picked row.
function modelWillUse(sel, custom, md) {
  if (modelIsDefault(sel, md)) {
    const d = defaultModel(md)
    return d ? 'the default model, ' + modelTitle(d) + (modelFacts(d) ? ' (' + modelFacts(d) + ')' : '') : 'the default model (the model set in Settings, since no saved choice is the default)'
  }
  if (sel.indexOf('m:') === 0) {
    const m = md.saved.find(x => x.id === sel.slice(2))
    return m ? modelTitle(m) + (modelFacts(m) ? ' (' + modelFacts(m) + ')' : '') : 'a saved model that no longer exists'
  }
  if (sel.indexOf('d:') === 0) {
    const d = md.detected[Number(sel.slice(2))]
    return d ? modelTitle(d) + (modelFacts(d) ? ' (' + modelFacts(d) + ')' : '') + ', from this Hermes profile' : 'a model from this Hermes profile'
  }
  const c = custom || {}
  return nz(c.model) ? c.model + (nz(c.url) ? ' at ' + hostOfUrl(c.url) : '') + ' (the endpoint typed below)' : 'the endpoint typed below'
}

function modelFacts(m) {
  const where = (m && (m.endpoint_host || hostOfUrl(m.base_url))) || ''
  const model = (m && m.model) || ''
  return model && where ? model + ' at ' + where : model || where
}

function sameEndpoint(a, b) {
  const norm = u => String(u || '').trim().replace(/\/+$/, '').toLowerCase()
  return norm(a.base_url) === norm(b.base_url) && String(a.model || '').trim() === String(b.model || '').trim()
}

// What a chosen model needs before the run starts: nothing ({}), an id ({model_id}), a
// choice to save first ({save}) or a sentence for the user ({error}).
function resolveModelChoice(sel, custom, md) {
  const choice = sel || ''
  if (!choice) return {}
  if (choice.indexOf('m:') === 0) {
    const id = choice.slice(2)
    return md.saved.some(m => m.id === id) ? { model_id: id } : { error: 'The chosen saved model no longer exists. Pick another model.' }
  }
  let entry
  if (choice.indexOf('d:') === 0) {
    const d = md.detected[Number(choice.slice(2))]
    if (!d || !d.base_url || !d.model) return { error: 'That model from this Hermes profile cannot be used. Pick another model.' }
    entry = { name: d.name, base_url: d.base_url, model: d.model, key_secret: d.key_secret }
  } else {
    const c = custom || {}
    entry = { name: nz(c.name), base_url: nz(c.url), model: nz(c.model), key_secret: nz(c.key) }
    if (!entry.base_url || !/^https?:\/\/[^\s@]+$/i.test(entry.base_url)) return { error: 'Enter the model endpoint as an http or https address, for example https://llm.example.net/v1.' }
    if (!entry.model) return { error: 'Enter the model id, for example qwen3-coder.' }
  }
  const same = md.saved.find(m => sameEndpoint(m, entry))
  if (same) return { model_id: same.id }
  const save = {}
  ;['name', 'base_url', 'model', 'key_secret'].forEach(k => {
    if (entry[k]) save[k] = entry[k]
  })
  return { save }
}

// The model id a run should use; saves a detected or typed choice first.
async function modelIdFor(sel, custom, md) {
  const pick = resolveModelChoice(sel, custom, md)
  if (pick.error) throw new Error(pick.error)
  if (pick.save) {
    const saved = await api('POST', '/agent-models', pick.save)
    invalidateAll()
    return saved && saved.id
  }
  return pick.model_id || null
}

function ModelRow({ selected, disabled, title, sub, badge, onSelect }) {
  return h(
    Button,
    {
      size: 'sm',
      variant: 'outline',
      disabled: !!disabled,
      className: cx('h-auto w-full justify-start whitespace-normal py-1.5 text-left', disabled ? 'opacity-60' : ''),
      onClick: onSelect
    },
    h(
      'span',
      { className: 'flex min-w-0 flex-col items-start gap-0.5' },
      h('span', { className: 'flex flex-wrap items-center gap-2' }, h('span', {}, selected ? '\u25cf' : '\u25cb'), h('span', { className: 'font-medium' }, title), badge ? h(Badge, { variant: 'muted' }, badge) : null),
      sub ? h('span', { className: cx('font-normal', SMALL, MUTED) }, sub) : null
    )
  )
}

// `value` is '' (default), 'm:<id>', 'd:<n>' or 'new'; `custom` holds the typed endpoint.
function ModelPicker({ q, value, custom, onChange }) {
  const onPick = v => onChange(v, undefined)
  const onCustom = c => onChange(undefined, c)
  const md = modelData(q && q.data)
  const act = useAction()
  const sel = value || ''
  const c = custom || {}
  const defaultSaved = defaultModel(md)
  const isDefault = modelIsDefault(sel, md)
  const chosen = isDefault ? defaultSaved : sel.indexOf('m:') === 0 ? md.saved.find(m => m.id === sel.slice(2)) : null
  const keyMissing = chosen && chosen.key_set === false
  const saveChoice = () =>
    act.run(async () => {
      const pick = resolveModelChoice('new', c, md)
      if (pick.error) throw new Error(pick.error)
      const saved = pick.save ? await api('POST', '/agent-models', pick.save) : { id: pick.model_id }
      toast('success', 'Saved the model choice' + (pick.save && saved && saved.name ? ' ' + saved.name : '') + '.')
      invalidateAll()
      onChange(saved && saved.id ? 'm:' + saved.id : sel, { name: '', url: '', model: '', key: '' })
    })
  return h(
    Field,
    { label: 'Model', hint: 'The model the agent works with. Pick a saved choice or one this Hermes profile already offers, or add another endpoint here. Nothing typed in this dialog is lost.' },
    h(
      'div',
      { className: 'flex flex-col gap-1.5' },
      h(ModelRow, {
        key: 'default',
        selected: isDefault,
        title: 'Default model',
        badge: defaultSaved ? modelTitle(defaultSaved) : null,
        sub: defaultSaved ? modelFacts(defaultSaved) : 'The default saved choice, else the model set in Settings. Add one below if none is set up.',
        onSelect: () => onPick('')
      }),
      // The default saved choice is the 'Default model' row above: it is not listed twice.
      md.saved.filter(m => !defaultSaved || m.id !== defaultSaved.id).map(m =>
        h(ModelRow, {
          key: m.id,
          selected: sel === 'm:' + m.id,
          title: modelTitle(m),
          badge: 'saved',
          sub: modelFacts(m) + (m.key_set === false ? '. Key not set (secret ' + (m.key_secret || AGENT_KEY_ENV) + ')' : ''),
          onSelect: () => onPick('m:' + m.id)
        })
      ),
      md.detected.map((d, i) =>
        h(ModelRow, {
          key: 'd' + i,
          selected: sel === 'd:' + i,
          title: modelTitle(d),
          badge: 'from this Hermes profile',
          sub: modelFacts(d) + (d.key_set === false ? '. Key not set' : ''),
          onSelect: () => onPick('d:' + i)
        })
      ),
      md.unavailable.map((u, i) =>
        h(ModelRow, {
          key: 'u' + i,
          disabled: true,
          selected: false,
          title: modelTitle(u),
          badge: 'not available',
          sub: (u.reason ? u.reason : 'This entry cannot be used for cloud agents.') + (modelFacts(u) ? ' (' + modelFacts(u) + ')' : ''),
          onSelect: () => undefined
        })
      ),
      h(ModelRow, { key: 'new', selected: sel === 'new', title: 'Another endpoint\u2026', sub: 'Type an endpoint and model id. It is saved so it can be picked again.', onSelect: () => onPick('new') }),
      sel === 'new'
        ? h(
            'div',
            { className: 'flex flex-col gap-2 rounded-md border border-(--ui-stroke-secondary) p-2.5' },
            h(TextField, { label: 'Endpoint', value: c.url, onChange: v => onCustom(Object.assign({}, c, { url: v })), placeholder: 'https://llm.example.net/v1', hint: 'An http or https address for an OpenAI-style API.' }),
            h(TextField, { label: 'Model id', value: c.model, onChange: v => onCustom(Object.assign({}, c, { model: v })), placeholder: 'qwen3-coder' }),
            h(TextField, { label: 'Name (optional)', value: c.name, onChange: v => onCustom(Object.assign({}, c, { name: v })), placeholder: 'Blank uses the model id and endpoint' }),
            h(TextField, { label: 'Key secret name (optional)', value: c.key, onChange: v => onCustom(Object.assign({}, c, { key: v })), placeholder: AGENT_KEY_ENV, hint: 'The name of the Hermes secret that holds the key, never the key itself. Blank uses ' + AGENT_KEY_ENV + '.' }),
            h(ErrorLine, { message: act.error }),
            h('div', {}, h(Button, { size: 'xs', variant: 'outline', disabled: act.busy, onClick: saveChoice }, act.busy ? 'Saving\u2026' : 'Save this choice'))
          )
        : null,
      keyMissing
        ? h(
            'div',
            { className: cx('flex flex-wrap items-center gap-2', SMALL, SECONDARY) },
            'The key for this model (secret ' + (chosen.key_secret || AGENT_KEY_ENV) + ') is not set yet. Add it in Settings; this draft is kept.',
            h(Button, { size: 'xs', variant: 'outline', onClick: () => host.navigate(pluginSettingsHref(PLUGIN_ID)) }, 'Open settings')
          )
        : null
    )
  )
}

function buildAgentRequest(f) {
  const goal = String(f.goal || '').trim()
  if (!goal) return { error: 'Describe what the agent should do.' }
  if (!f.host_id) return { error: 'Choose a host.' }
  const repo = cleanPath(f.repo_path)
  if (!repo) return { error: 'Enter the path of the git repository on this computer.' }
  let turns = 30
  if (String(f.max_turns == null ? '' : f.max_turns).trim()) {
    turns = numOrNull(f.max_turns)
    if (turns == null || !Number.isInteger(turns) || turns < 1) return { error: 'Max turns must be a whole number of at least 1.' }
  }
  const fresh = f.mode !== 'existing'
  const body = {
    goal,
    host_id: f.host_id,
    repo_path: repo,
    new_branch: fresh,
    template: f.template || 'hermes-agent',
    max_turns: turns
  }
  const branch = nz(f.branch)
  if (branch) body.branch = branch
  const base = nz(f.base)
  if (fresh && base) body.base = base
  // Sent only when filled in: the optional 'Done when' line.
  const criteria = nz(f.criteria)
  if (criteria) body.criteria = criteria
  // Sent only when a saved model was chosen; otherwise the backend picks the default.
  if (nz(f.model_id)) body.model_id = nz(f.model_id)
  return { body }
}

function NewAgentDialog({ hosts, templates, onClose, onCreated }) {
  const eligible = (Array.isArray(hosts) ? hosts : []).filter(i => i && i.spec && i.spec.kind !== 'slurm' && i.spec.limits && i.spec.limits.allow_agents)
  const found = (Array.isArray(templates) ? templates : []).filter(t => t && t.agent && typeof t.agent === 'object' && t.agent.enabled !== false)
  // Model-backed templates first; the no-model handoff test last.
  const choices = (found.length ? found : AGENT_TEMPLATE_FALLBACK).slice().sort((a, b) => Number(agentIsScript(a)) - Number(agentIsScript(b)))
  loadAgentDraft()
  const stored = useValue($agentDraft)
  const draft = Object.assign({}, DRAFT_DEFAULTS, stored || {})
  // Every edit goes straight into the module-level draft, so nothing typed is lost.
  const edit = part => saveAgentDraft(Object.assign({}, draft, part, { origin: '' }))
  // Said once, when the dialog opens on a draft that already holds something.
  const [opened] = useState(() => (draftHasContent(stored) ? stored.origin || 'draft' : ''))
  const [dismissed, setDismissed] = useState(false)
  const [localError, setLocalError] = useState('')
  const act = useAction()
  const key = useQueryKey()
  const modelsQ = useQuery({ queryKey: key('agent-models'), queryFn: () => api('GET', '/agent-models'), retry: false })

  const hostId = eligible.some(i => i.spec.id === draft.host) ? draft.host : eligible[0] ? eligible[0].spec.id : ''
  const preferred = choices.find(t => t.id === 'hermes-agent') || choices.find(t => !agentIsScript(t)) || choices[0]
  const tplId = choices.some(t => t.id === draft.template) ? draft.template : preferred.id
  const chosenTemplate = choices.find(t => t.id === tplId) || preferred

  const submit = () => {
    const built = buildAgentRequest({ goal: draft.goal, criteria: draft.criteria, host_id: hostId, repo_path: draft.repo, mode: draft.mode, branch: draft.branch, base: draft.base, template: tplId, max_turns: draft.turns })
    if (built.error) {
      setLocalError(built.error)
      return
    }
    setLocalError('')
    const custom = { name: draft.ep_name, url: draft.ep_url, model: draft.ep_model, key: draft.ep_key }
    act.run(async () => {
      // A model picked from this Hermes profile or typed here is saved first.
      const modelId = agentIsScript(chosenTemplate) ? null : await modelIdFor(draft.model, custom, modelData(modelsQ.data))
      const body = Object.assign({}, built.body)
      if (modelId) body.model_id = modelId
      const run = await api('POST', '/agents', body)
      clearAgentDraft()
      toast('success', 'Started cloud agent ' + ((run && run.id) || '') + '.')
      invalidateAll()
      if (onCreated) onCreated(run)
    })
  }

  return h(
    Modal,
    { open: true, onClose, title: 'New agent run', description: AGENT_BLURB, className: 'max-h-[85vh] overflow-y-auto' },
    h(
      'div',
      { className: 'flex flex-col gap-3' },
      opened && !dismissed
        ? h(
            'div',
            { className: cx('flex flex-wrap items-center justify-between gap-2 rounded-md border border-(--ui-stroke-secondary) px-2.5 py-1.5', SMALL, SECONDARY) },
            h('span', {}, opened === 'draft' ? 'Draft restored' : 'Filled in from ' + opened + '. Change what is needed, then Start.'),
            h(
              Button,
              {
                size: 'xs',
                variant: 'ghost',
                onClick: () => {
                  clearAgentDraft()
                  setDismissed(true)
                  setLocalError('')
                }
              },
              'Discard draft'
            )
          )
        : null,
      h(
        Field,
        { label: 'Task for the agent', hint: 'Describe what the agent should do in the repository. It works on its own until the task is done or the turn limit is reached.' },
        h(Textarea, { value: draft.goal, rows: 4, placeholder: 'For example: add a --verbose flag to the command line tool and cover it with a test.', onChange: ev => edit({ goal: ev.target.value }) })
      ),
      h(TextField, { label: 'Done when (optional)', value: draft.criteria, onChange: text(v => edit({ criteria: v })), placeholder: 'For example: all tests pass and the README mentions --verbose.', hint: 'How the agent can tell the task is complete.' }),
      eligible.length
        ? h(Field, { label: 'Host' }, h(Pick, { value: hostId, onChange: v => edit({ host: plain(v) }), options: eligible.map(i => ({ value: i.spec.id, label: hostChoiceLabel(i) })) }))
        : h(
            'div',
            { className: cx('flex flex-wrap items-center gap-2', SMALL, MUTED) },
            'No host allows cloud agents yet. Turn on "Allow cloud agents" for an SSH or local host.',
            h(Button, { size: 'xs', variant: 'outline', onClick: () => setIntent('hosts') }, 'Open Hosts')
          ),
      h(TextField, { label: 'Repository path on this computer', value: draft.repo, onChange: text(v => edit({ repo: v })), placeholder: '/home/you/code/project', hint: 'A git repository with at least one commit. Only this branch is sent to the sandbox.' }),
      h(SegmentedControl, {
        value: draft.mode,
        onChange: v => edit({ mode: v }),
        options: [
          { id: 'new', label: 'New branch' },
          { id: 'existing', label: 'Existing branch' }
        ]
      }),
      h(
        'div',
        { className: 'grid gap-2 sm:grid-cols-2' },
        h(TextField, {
          label: draft.mode === 'new' ? 'Branch name (optional)' : 'Branch (optional)',
          value: draft.branch,
          onChange: text(v => edit({ branch: v })),
          placeholder: draft.mode === 'new' ? 'cloud/my-change' : 'main',
          hint: draft.mode === 'new' ? 'Blank picks a name from the task.' : 'Blank continues the branch checked out in the repository.'
        }),
        draft.mode === 'new' ? h(TextField, { label: 'Base', value: draft.base, onChange: text(v => edit({ base: v })), placeholder: 'HEAD', hint: 'The commit the new branch starts from.' }) : null
      ),
      h(
        'div',
        { className: 'grid gap-2 sm:grid-cols-2' },
        h(
          Field,
          { label: 'Template', hint: agentIsScript(chosenTemplate) ? HANDOFF_HELP : undefined },
          h(Pick, { value: tplId, onChange: v => edit({ template: plain(v) }), options: choices.map(t => ({ value: t.id, label: agentTemplateLabel(t) })) })
        ),
        h(TextField, { label: 'Max turns', value: draft.turns, onChange: text(v => edit({ turns: v })), type: 'number', hint: 'Stops the agent after this many model turns.' })
      ),
      agentIsScript(chosenTemplate)
        ? null
        : h(ModelPicker, {
            q: modelsQ,
            value: draft.model,
            custom: { name: draft.ep_name, url: draft.ep_url, model: draft.ep_model, key: draft.ep_key },
            onChange: (sel, c) => edit(Object.assign({}, sel !== undefined ? { model: sel } : {}, c ? { ep_name: c.name, ep_url: c.url, ep_model: c.model, ep_key: c.key } : {}))
          }),
      h(AgentError, { message: localError || act.error, code: localError ? '' : act.code }),
      h(
        'div',
        { className: 'flex justify-end gap-2' },
        h(Button, { variant: 'ghost', onClick: onClose }, 'Cancel'),
        h(Button, { disabled: act.busy || !eligible.length, onClick: submit }, act.busy ? 'Starting\u2026' : 'Start')
      )
    )
  )
}

// Where a refused Collect kept the sandbox commits: the core names it in the message
// ("... are kept as refs/cloud-compute/incoming/..."); a result may also carry it as `incoming_ref`.
function incomingRef(info) {
  if (typeof info.incoming_ref === 'string' && info.incoming_ref) return info.incoming_ref
  const m = typeof info.message === 'string' ? /refs\/cloud-compute\/incoming\/[^\s,;()]+/.exec(info.message) : null
  return m ? m[0].replace(/[.]+$/, '') : ''
}

function CollectInfo({ info, fresher, stopNote }) {
  const conflicts = Array.isArray(info.conflicts_dry_run) ? info.conflicts_dry_run : []
  const blocked = info.status === 'blocked'
  const message = typeof info.message === 'string' ? info.message.trim() : ''
  const incoming = incomingRef(info)
  const row = (label, value, copy) =>
    h(
      'div',
      { key: label, className: 'flex flex-wrap items-center gap-2' },
      h('span', { className: MUTED }, label),
      h('code', { className: 'break-all' }, value),
      copy ? h(CopyButton, { text: copy, label: 'Copy', size: 'sm' }) : null
    )
  return h(
    'div',
    { className: cx('flex flex-col gap-1 rounded-md border border-(--ui-stroke-secondary) p-2', SMALL) },
    h('div', { className: 'font-medium' }, 'Results fetched'),
    row('Branch', info.ref || '?', info.ref || null),
    info.worktree_path ? row('Worktree', info.worktree_path, info.worktree_path) : h('div', { key: 'wt', className: MUTED }, 'No worktree was created.'),
    Number.isFinite(info.commits_ahead) ? h('div', { key: 'ahead' }, info.commits_ahead + ' ' + plural(info.commits_ahead, 'commit', 'commits') + ' ahead of where the run started') : null,
    blocked ? h('div', { key: 'blocked', className: 'font-medium text-destructive' }, 'Blocked: your branch could not be updated.') : null,
    info.diverged ? h('div', { key: 'div', className: 'text-(--ui-text-secondary)' }, 'Diverged: your branch has new commits too.') : null,
    message ? h('div', { key: 'msg', className: 'break-words text-destructive' }, message) : null,
    incoming ? row('Kept as', incoming, incoming) : null,
    conflicts.length
      ? h('div', { key: 'conf', className: 'text-destructive' }, 'Merging would conflict in: ' + conflicts.join(', '))
      : info.diverged
        ? h('div', { key: 'conf', className: MUTED }, 'A trial merge shows no conflicts.')
        : null,
    fresher ? h('div', { key: 'fresh', className: 'font-medium' }, 'The agent has new commits since these results were fetched.') : null,
    stopNote ? h('div', { key: 'stopnote', className: 'font-medium' }, stopNote) : null,
    h('div', { key: 'note', className: MUTED }, 'Nothing was merged. Review the branch, then merge it yourself.')
  )
}

const GET_RESULTS_HELP = "Brings the agent's branch to this computer as a new branch and worktree in your repository. Nothing is merged."

// ---- the agent's reply, the wait, a failure, a follow-up ---------------------------
// An agent reply can start with box-drawing diff lines ('│ NOTES.md', '│ @@ ...'). They are
// shown as they are, in a monospace block, so the columns line up; nothing is cut out.
function replyHasBoxLines(text) {
  return /^[\s]*[\u2500-\u257f]/m.test(String(text || ''))
}

const REPLY_LIMIT_CHARS = 700
const REPLY_LIMIT_LINES = 12

// A long reply is cut for the card; 'Show all' reveals the rest.
function replyView(text) {
  const lines = String(text || '').split('\n')
  if (text.length <= REPLY_LIMIT_CHARS && lines.length <= REPLY_LIMIT_LINES) return { text, cut: false }
  let out = lines.slice(0, REPLY_LIMIT_LINES).join('\n')
  if (out.length > REPLY_LIMIT_CHARS) out = out.slice(0, REPLY_LIMIT_CHARS)
  return { text: out.replace(/\s+$/, '') + '\u2026', cut: true }
}

function AgentReply({ run }) {
  const [all, setAll] = useState(false)
  const live = AGENT_ACTIVE.indexOf(run.status) >= 0
  const reply = typeof run.reply === 'string' && !agentFailedBeforeWork(run) ? maskText(run.reply.trim()) : ''
  if (live) {
    return h(
      'div',
      { className: cx('flex flex-col gap-1.5 rounded-md border border-(--ui-stroke-secondary) p-2', SMALL) },
      h('span', { className: 'flex items-center gap-2 font-medium' }, h(StatusDot, { tone: 'good' }), run.status === 'starting' ? 'Starting the agent\u2026' : (run.turns > 1 ? 'Working on your message (turn ' + run.turns + ')\u2026' : 'Working on the task\u2026')),
      h('span', { className: SECONDARY }, "Waiting for the agent's reply. This card updates by itself when the turn ends; there is nothing to refresh."),
      reply ? h('span', { className: MUTED }, 'The reply of the earlier turn is hidden until the new one arrives.') : null
    )
  }
  if (!reply) return null
  const view = replyView(reply)
  return h(
    'div',
    { className: cx('flex flex-col gap-1 rounded-md border border-(--ui-stroke-secondary) p-2', SMALL) },
    h('div', { className: 'font-medium' }, "Agent's reply"),
    h('div', { className: cx('whitespace-pre-wrap break-words', replyHasBoxLines(reply) ? 'font-mono' : '') }, all || !view.cut ? reply : view.text),
    view.cut ? h('div', {}, h(Button, { size: 'xs', variant: 'ghost', onClick: () => setAll(!all) }, all ? 'Show less' : 'Show all')) : null
  )
}

const FAILED_NEXT =
  'Read the cause above. Press Run again\u2026 to start the task again (you can change the model or host first), or send the agent a message below to try again in the same sandbox.'

function AgentFailure({ run }) {
  if (run.status !== 'failed') return null
  const cause = run.error ? maskText(String(run.error)) : 'The run failed without a reason.'
  const next = typeof run.next === 'string' && run.next.trim() ? run.next.trim() : FAILED_NEXT
  return h(
    'div',
    { className: cx('flex flex-col gap-1 rounded-md border border-(--ui-stroke-secondary) p-2', SMALL) },
    h('div', { className: 'font-medium text-destructive' }, 'The run failed'),
    h('div', { className: 'break-words text-destructive' }, cause),
    h('div', { className: 'break-words' }, h('span', { className: 'font-medium' }, 'What to do: '), next)
  )
}

// Can this run take a message? A reason is given when it cannot.
function agentFollowUp(run, instances) {
  if (AGENT_ACTIVE.indexOf(run && run.status) >= 0) return { can: false, reason: 'The agent is still working on its turn. Wait for it to finish, or stop it.' }
  if (run && run.status === 'stopped') return { can: false, reason: 'This run was stopped and its sandbox is gone. Use Run again\u2026 to start a new run.' }
  if (agentSandboxGone(run, instances)) return { can: false, reason: "This run's sandbox is gone, so it cannot take a message. Use Run again\u2026 to start a new run." }
  return { can: true, reason: '' }
}

function FollowUpBox({ run, instances }) {
  const act = useAction()
  const [message, setMessage] = useState('')
  const rule = agentFollowUp(run, instances)
  const send = () => {
    const body = message.trim()
    if (!body) return undefined
    return act.run(async () => {
      await api('POST', '/agents/' + encodeURIComponent(run.id) + '/message', { message: body })
      setMessage('')
      toast('success', 'Message sent to ' + run.id + '. The reply appears on this card when the turn ends.')
      invalidateAll()
    })
  }
  return h(
    'div',
    { className: 'flex flex-col gap-1.5' },
    h(Field, { label: 'Send a message to this agent', hint: rule.can ? 'The agent continues in the same sandbox and branch. Its next reply replaces the one above.' : rule.reason }, h(Textarea, { value: message, rows: 2, disabled: !rule.can || act.busy, placeholder: rule.can ? 'For example: also cover the empty-input case with a test.' : '', onChange: ev => setMessage(ev.target.value) })),
    h(AgentError, { message: act.error, code: act.code }),
    h('div', {}, h(Button, { size: 'xs', disabled: !rule.can || act.busy || !message.trim(), onClick: send }, act.busy ? 'Sending\u2026' : 'Send message'))
  )
}

// Model and endpoint of the run, and how many prompts it has had.
// A run whose model call failed before the agent did anything: no commits, no turn.
// It stays so after Stop: the card keeps the cause sentence and never the provider's raw reply.
const AGENT_MODEL_FAILURES = ['model_not_found', 'bad_key', 'unreachable', 'rate_limit', 'server', 'context', 'endpoint']
function agentFailedBeforeWork(run) {
  return !!run && (run.status === 'failed' || run.status === 'stopped') && AGENT_MODEL_FAILURES.indexOf(run.error_kind) >= 0 && !((run.commits_ahead || 0) > 0) && !((run.turns || 1) > 1)
}

function agentFacts(run) {
  const model = run.model ? (run.endpoint_host ? run.model + ' at ' + run.endpoint_host : run.model) : run.endpoint_host || ''
  const turns = !agentFailedBeforeWork(run) && Number.isFinite(run.turns) && run.turns > 0 ? run.turns + ' ' + plural(run.turns, 'turn', 'turns') : null
  return [model ? 'Model ' + model : null, turns].filter(Boolean)
}

// 'Run again' with the backend's rerun route: the same task on a new run, with the host
// or the model changed first. Without that route (a 404) the New agent run form is
// filled in from the old run instead.
function RerunDialog({ run, hosts, onClose, onFallback }) {
  const key = useQueryKey()
  const q = useQuery({ queryKey: key('agent-models'), queryFn: () => api('GET', '/agent-models'), retry: false })
  const eligible = (Array.isArray(hosts) ? hosts : []).filter(i => i && i.spec && i.spec.kind !== 'slurm' && i.spec.limits && i.spec.limits.allow_agents)
  const [hostId, setHostId] = useState(run.host_id || '')
  const [sel, setSel] = useState(run.model_id ? 'm:' + run.model_id : '')
  const [custom, setCustom] = useState({ name: '', url: '', model: '', key: '' })
  const act = useAction()
  const scripted = run.mode === 'script'
  const md = modelData(q.data)
  const start = () =>
    act.run(async () => {
      const body = {}
      if (hostId && hostId !== run.host_id) body.host_id = hostId
      if (!scripted) {
        if (modelIsDefault(sel, md)) {
          // 'Default model' means the default as it is now, not the model of the old run: the
          // backend would otherwise reuse the old run's model when nothing is sent.
          body.model_id = md.defaultId || DEFAULT_MODEL_MARKER
        } else {
          const modelId = await modelIdFor(sel, custom, md)
          if (modelId && modelId !== run.model_id) body.model_id = modelId
        }
      }
      let fresh
      try {
        fresh = await api('POST', '/agents/' + encodeURIComponent(run.id) + '/rerun', body)
      } catch (e) {
        if (errInfo(e).status === 404) {
          toast('info', 'Starting a run again directly is not available here, so the New agent run form was filled in instead.')
          onFallback(run)
          return
        }
        throw e
      }
      toast('success', 'Started cloud agent ' + ((fresh && fresh.id) || '') + '.')
      invalidateAll()
      onClose()
    })
  return h(
    Modal,
    { open: true, onClose, title: 'Run ' + run.id + ' again', description: 'Starts a new run with the same task, repository and branch settings. Change the host or the model first if the old choice was the problem.', className: 'max-h-[85vh] overflow-y-auto' },
    h(
      'div',
      { className: 'flex flex-col gap-3' },
      run.goal ? h('div', { className: cx('whitespace-pre-wrap rounded-md border border-(--ui-stroke-secondary) p-2', SMALL, SECONDARY) }, run.goal) : null,
      eligible.length ? h(Field, { label: 'Host' }, h(Pick, { value: hostId, onChange: v => setHostId(plain(v)), options: eligible.map(i => ({ value: i.spec.id, label: hostChoiceLabel(i) })) })) : null,
      scripted ? h('div', { className: cx(SMALL, MUTED) }, HANDOFF_HELP) : h('div', { className: cx('font-medium', SMALL) }, 'Model for this run: ' + modelWillUse(sel, custom, md)),
      scripted ? null : h(ModelPicker, { q, value: sel, custom, onChange: (v, c) => { if (v !== undefined) setSel(v); if (c) setCustom(c) } }),
      h(AgentError, { message: act.error, code: act.code }),
      h(
        'div',
        { className: 'flex flex-wrap justify-between gap-2' },
        h(Button, { variant: 'ghost', onClick: () => onFallback(run) }, 'Change everything\u2026'),
        h(
          'div',
          { className: 'flex gap-2' },
          h(Button, { variant: 'ghost', onClick: onClose }, 'Cancel'),
          h(Button, { disabled: act.busy, onClick: start }, act.busy ? 'Starting\u2026' : 'Run again')
        )
      )
    )
  )
}

const STOP_AGENT_HELP =
  'The agent stops. Commits it made that are not on this computer yet are fetched first, then the files in /work are saved to this computer and the sandbox is removed. If fetching fails, nothing is deleted.'

// What the toast says after Stop: what was fetched, or where unfetched commits are.
function stoppedText(run, res) {
  const note = res && typeof res.stop_note === 'string' ? res.stop_note.trim() : ''
  return 'Stopped ' + run.id + '.' + (note ? ' ' + note : '')
}

function AgentRunCard({ run, instances, hosts, onRunAgain }) {
  const act = useAction()
  const [result, setResult] = useState(null)
  const [confirming, setConfirming] = useState(false)
  const [removing, setRemoving] = useState(false)
  const [again, setAgain] = useState(false)
  // Set when Stop could not fetch the agent's newest commits and kept the sandbox: the text
  // of the second, separate confirmation to delete it anyway.
  const [anyway, setAnyway] = useState(null)
  const seen = fetchedHeads()
  const actions = agentActions(run, instances, seen)
  const stopNote = typeof run.stop_note === 'string' ? run.stop_note.trim() : ''
  const state = agentResults(run, seen)
  const lines = Array.isArray(run.last_log) ? run.last_log : []
  const live = AGENT_ACTIVE.indexOf(run.status) >= 0
  const hasReply = typeof run.reply === 'string' && run.reply.trim() !== ''
  const sandboxGone = agentSandboxGone(run, instances)
  const meta = [
    'Host ' + (run.host_id || '?'),
    run.branch ? 'Branch ' + run.branch : null,
    Number.isFinite(run.commits_ahead) ? run.commits_ahead + ' ' + plural(run.commits_ahead, 'commit', 'commits') + ' ahead' : null,
    run.started_at ? 'Started ' + ago(run.started_at) : null,
    run.ended_at ? 'Ended ' + ago(run.ended_at) : null
  ]
    .concat(agentFacts(run))
    .filter(Boolean)
  const info = result || (run.collected_ref ? { ref: run.collected_ref, worktree_path: run.worktree_path, commits_ahead: run.commits_ahead } : null)
  // Right after a fetch the run record may not have refreshed yet: nothing new to get.
  const justFetched = !!result && !!result.worktree_path && result.status !== 'blocked'
  const canFetch = actions.indexOf('collect') >= 0 && !justFetched

  // Remember what was fetched, so later commits read as 'new results'; a changed status
  // makes the run record the truth again.
  useEffect(() => {
    if (run.status === 'collected' && run.head_sha) rememberFetched(run.id, run.head_sha)
    setResult(null)
  }, [run.status, run.head_sha])

  const collect = () =>
    act.run(async () => {
      const res = await api('POST', '/agents/' + encodeURIComponent(run.id) + '/collect', { into: 'worktree' })
      setResult(res)
      const note = res && typeof res.message === 'string' ? res.message : ''

      if (res && res.diverged) {
        const kept = note.match(/refs\/cloud-compute\/incoming\/\S+/)
        const ref = kept ? kept[0].replace(/[.,;]+$/, '') : res.ref || 'an incoming ref'

        toast('warning', run.id + ": collected, but your branch has diverged; the agent's version was kept as " + ref + '.')
      } else if (note) {
        toast('warning', run.id + ': ' + (note.length > 160 ? note.slice(0, 157) + '...' : note))
      } else {
        toast('success', 'Collected ' + run.id + ' into ' + ((res && res.ref) || 'your repository') + '.')
      }
      invalidateAll()
    })

  const removeTip = live
    ? 'An active run must be stopped first. Stop it, then remove the record.'
    : !sandboxGone
      ? 'Stop the run first. Stop saves the files in /work to this computer, then deletes the sandbox; after that the record can be removed.'
      : ''

  return h(
    'div',
    { className: CARD },
    h(
      'div',
      { className: 'flex flex-wrap items-center justify-between gap-2' },
      h(
        'div',
        { className: 'flex min-w-0 items-center gap-2' },
        h(StatusDot, { tone: agentTone(run.status) }),
        h('span', { className: 'truncate text-sm font-semibold' }, run.id),
        h(Badge, { variant: agentBadge(run.status) }, run.status || 'unknown'),
        run.mode ? h(Badge, { variant: 'outline' }, run.mode === 'script' ? 'handoff test' : run.mode) : null
      ),
      h('span', { className: cx(SMALL, MUTED) }, run.template_id || '')
    ),
    run.goal ? h('div', { className: cx('whitespace-pre-wrap', SMALL) }, run.goal) : null,
    h('div', { className: cx('flex flex-wrap gap-x-4 gap-y-0.5', SMALL, SECONDARY) }, meta.map(m => h('span', { key: m }, m))),
    h(AgentFailure, { run }),
    run.status !== 'failed' && run.error ? h('div', { className: cx('text-destructive', SMALL) }, maskText(run.error)) : null,
    h(AgentReply, { run }),
    lines.length && (live || !hasReply) ? h(LogView, { className: 'max-h-48' }, lines.join('\n')) : null,
    run.mode === 'script' && !live ? h('div', { className: cx(SMALL, MUTED) }, 'Handoff test: no model was used.') : null,
    info ? h(CollectInfo, { info, fresher: state === 'new' && !result && !sandboxGone, stopNote }) : stopNote ? h('div', { className: cx('font-medium', SMALL) }, stopNote) : null,
    h(AgentError, { message: act.error, code: act.code }),
    canFetch ? h('div', { className: cx(SMALL, MUTED) }, GET_RESULTS_HELP) : null,
    run.status !== 'stopped' || live ? h(FollowUpBox, { run, instances }) : null,
    h(
      'div',
      { className: 'flex flex-wrap gap-1.5' },
      canFetch ? h(Button, { key: 'collect', size: 'xs', disabled: act.busy, onClick: collect }, act.busy ? 'Working\u2026' : state === 'none' ? 'Get results' : 'Get new results') : null,
      actions.indexOf('stop') >= 0 ? h(Button, { key: 'stop', size: 'xs', variant: 'outline', disabled: act.busy, onClick: () => setConfirming(true) }, 'Stop') : null,
      !live ? h(Button, { key: 'again', size: 'xs', variant: 'ghost', disabled: act.busy, onClick: () => setAgain(true) }, 'Run again\u2026') : null,
      removeTip
        ? h(Tip, { key: 'remove', label: removeTip }, h('span', {}, h(Button, { size: 'xs', variant: 'ghost', disabled: true }, 'Remove')))
        : h(Button, { key: 'remove', size: 'xs', variant: 'ghost', disabled: act.busy, onClick: () => setRemoving(true) }, 'Remove')
    ),
    removeTip ? h('div', { className: cx(SMALL, MUTED) }, removeTip) : null,
    again ? h(RerunDialog, { run, hosts, onClose: () => setAgain(false), onFallback: r => { setAgain(false); if (onRunAgain) onRunAgain(r) } }) : null,
    removing
      ? h(ConfirmDialog, {
          open: true,
          destructive: true,
          title: 'Remove ' + run.id + ' from the list?',
          description: 'Only the record of this run is removed. Branches and worktrees already fetched to this computer are not changed.',
          confirmLabel: 'Remove',
          onClose: () => setRemoving(false),
          onConfirm: async () => {
            let res
            try {
              res = await api('DELETE', '/agents/' + encodeURIComponent(run.id))
            } catch (e) {
              throw new Error(errInfo(e).message)
            }
            toast('success', (res && res.note) || 'Removed ' + run.id + '.')
            invalidateAll()
          }
        })
      : null,
    confirming
      ? h(ConfirmDialog, {
          open: true,
          destructive: true,
          title: 'Stop ' + run.id + '?',
          description: STOP_AGENT_HELP,
          confirmLabel: 'Stop',
          onClose: () => setConfirming(false),
          onConfirm: async () => {
            let res
            try {
              res = await api('POST', '/agents/' + encodeURIComponent(run.id) + '/stop', { keep_branch: true })
            } catch (e) {
              const info = errInfo(e)
              if (info.code === 'not_fetched') {
                // Nothing was deleted. Ask again, with the cause, before deleting anyway.
                invalidateAll()
                setConfirming(false)
                setAnyway(info.message)
                return
              }
              throw new Error(info.message)
            }
            toast('success', stoppedText(run, res))
            invalidateAll()
          }
        })
      : null,
    anyway
      ? h(ConfirmDialog, {
          open: true,
          destructive: true,
          title: 'Delete the sandbox of ' + run.id + ' anyway?',
          description: anyway,
          confirmLabel: 'Delete anyway',
          onClose: () => setAnyway(null),
          onConfirm: async () => {
            let res
            try {
              res = await api('POST', '/agents/' + encodeURIComponent(run.id) + '/stop', { keep_branch: true, delete_anyway: true })
            } catch (e) {
              throw new Error(errInfo(e).message)
            }
            toast('warning', stoppedText(run, res))
            setAnyway(null)
            invalidateAll()
          }
        })
      : null
  )
}

function AgentsTab() {
  const key = useQueryKey()
  const intent = useValue($intent)
  const [creating, setCreating] = useState(false)
  // Run again on a run while a draft with text exists asks before it replaces the draft.
  const [replacing, setReplacing] = useState(null)
  const runsQ = useQuery({ queryKey: key('agents'), queryFn: () => api('GET', '/agents'), refetchInterval: POLL_MS, retry: 1 })
  const hostsQ = useQuery({ queryKey: key('hosts'), queryFn: () => api('GET', '/hosts'), retry: 1 })
  const tplQ = useQuery({ queryKey: key('templates'), queryFn: () => api('GET', '/templates'), retry: 1 })
  // Which sandboxes still exist: decides whether a finished run can be collected or stopped.
  const instQ = useQuery({ queryKey: key('instances'), queryFn: () => api('GET', '/instances'), refetchInterval: POLL_MS, retry: 1 })

  useEffect(() => {
    if (intent && intent.action === 'new-agent') {
      setCreating(true)
      $intent.set(null)
    }
  }, [intent])

  const fillFrom = run => {
    saveAgentDraft(draftFromRun(run))
    setReplacing(null)
    setCreating(true)
  }
  const runAgain = run => {
    loadAgentDraft()
    if (draftHasContent(readAtom($agentDraft))) setReplacing(run)
    else fillFrom(run)
  }

  const newButton = h(Button, { size: 'sm', onClick: () => setCreating(true) }, 'New agent run\u2026')

  return h(
    'div',
    { className: 'flex flex-col gap-3' },
    h(
      'div',
      { className: 'flex items-center justify-between gap-2' },
      h('div', { className: cx(SMALL, SECONDARY) }, 'Hermes agents working on your git branches in sandboxes.'),
      newButton
    ),
    h(Async, { q: runsQ, label: 'Loading agent runs\u2026' }, data => {
      const list = Array.isArray(data) ? data : []
      if (!list.length) {
        return h(
          'div',
          { className: 'flex flex-col items-center gap-2' },
          h(EmptyState, { title: 'No cloud agent runs yet', description: AGENT_BLURB }),
          newButton
        )
      }
      return h('div', { className: 'flex flex-col gap-3' }, list.map(run => h(AgentRunCard, { key: run.id, run, instances: instQ.data, hosts: hostsQ.data, onRunAgain: runAgain })))
    }),
    creating
      ? h(NewAgentDialog, {
          hosts: hostsQ.data,
          templates: tplQ.data,
          onClose: () => setCreating(false),
          onCreated: () => setCreating(false)
        })
      : null,
    replacing
      ? h(ConfirmDialog, {
          open: true,
          title: 'Replace your draft?',
          description: 'New agent run holds a draft that was not started. Run again replaces it with the settings of ' + replacing.id + '.',
          confirmLabel: 'Replace draft',
          onClose: () => setReplacing(null),
          onConfirm: () => fillFrom(replacing)
        })
      : null
  )
}

// ---------------------------------------------------------------------------
// Help tab
// ---------------------------------------------------------------------------

// Styles for the rendered Help text. The class is doubled so these rules win over
// the markdown renderer's own list classes. Lists sit inside the text margin:
// bullets and numbers are drawn in the padding, never to the left of the text.
const DOC_CSS = [
  '.cc-doc.cc-doc > :first-child { margin-top: 0; }',
  '.cc-doc.cc-doc p { margin: 0.5rem 0; line-height: 1.55; }',
  '.cc-doc.cc-doc ul, .cc-doc.cc-doc ol { margin: 0.5rem 0; padding-left: 1.5rem; list-style-position: outside; }',
  '.cc-doc.cc-doc ul { list-style-type: disc; }',
  '.cc-doc.cc-doc ol { list-style-type: decimal; }',
  '.cc-doc.cc-doc li { margin: 0.25rem 0; padding-left: 0.25rem; display: list-item; }',
  '.cc-doc.cc-doc li > p { margin: 0; }',
  '.cc-doc.cc-doc li ul, .cc-doc.cc-doc li ol { margin: 0.25rem 0; padding-left: 1.25rem; }',
  '.cc-doc.cc-doc li ul { list-style-type: circle; }',
  '.cc-doc.cc-doc pre { margin: 0.5rem 0; padding: 0.5rem 0.75rem; overflow-x: auto; border: 1px solid var(--ui-stroke-secondary); border-radius: 0.375rem; }',
  '.cc-doc.cc-doc :not(pre) > code { padding: 0.05rem 0.3rem; border: 1px solid var(--ui-stroke-secondary); border-radius: 0.25rem; font-size: 0.85em; }',
  '.cc-doc.cc-doc h3 { margin: 1.1rem 0 0.4rem; padding-bottom: 0.25rem; border-bottom: 1px solid var(--ui-stroke-secondary); font-size: 0.9375rem; font-weight: 600; line-height: 1.3; }'
].join('\n')

function DocDialog({ topic, title, onClose }) {
  const key = useQueryKey()
  const q = useQuery({
    queryKey: key('doc', topic),
    queryFn: () => api('GET', '/docs/' + encodeURIComponent(topic)),
    retry: false
  })
  const links = q.data && Array.isArray(q.data.links) ? q.data.links : []
  const onBodyClick = ev => {
    const a = ev && ev.target && ev.target.closest ? ev.target.closest('a') : null
    const href = a && a.getAttribute ? a.getAttribute('href') : null
    if (href) {
      ev.preventDefault()
      ctx.os.openExternal(href)
    }
  }
  const card = Object.keys(COPY.type_cards).map(k => COPY.type_cards[k]).find(c => c.learn_more_topic === topic)
  return h(
    Modal,
    { open: true, onClose, title: (q.data && q.data.title) || title || topic, className: 'max-h-[85vh] overflow-y-auto' },
    qState(q) === 'loading'
      ? h(Loading, {})
      : qState(q) === 'error'
        ? h(
            'div',
            { className: 'flex flex-col gap-2' },
            h(ErrorLine, { message: errInfo(q.error).message }),
            card ? h('div', { className: cx(SMALL, SECONDARY) }, card.one_liner + ' ' + card.when_to_use) : null,
            h('div', {}, h(Button, { size: 'sm', variant: 'outline', onClick: () => invalidateAll() }, 'Retry'))
          )
        : h(
            'div',
            { className: 'flex flex-col gap-3' },
            h('div', { className: 'cc-doc cc-doc text-sm', onClick: onBodyClick }, h('style', {}, DOC_CSS), h(Streamdown, {}, q.data.body_md || '')),
            links.length
              ? h(
                  'div',
                  { className: 'flex flex-col gap-1.5 border-t border-(--ui-stroke-secondary) pt-3' },
                  h('h3', { className: cx('text-xs font-semibold uppercase tracking-wide', SECONDARY) }, 'Related links'),
                  h('div', { className: cx(SMALL, MUTED) }, 'These open in your browser.'),
                  h(
                    'ul',
                    { className: 'flex flex-wrap gap-2' },
                    links.map(l => h('li', { key: l.url }, h(Button, { size: 'xs', variant: 'outline', title: l.url, onClick: () => ctx.os.openExternal(l.url) }, (l.label || l.url) + ' \u2197')))
                  )
                )
              : null
          )
  )
}

function HelpTab() {
  const key = useQueryKey()
  const { types } = useHostTypes()
  const docsQ = useQuery({ queryKey: key('docs'), queryFn: () => api('GET', '/docs'), retry: false, staleTime: 60000 })
  const [open, setOpen] = useState(null)
  const topics = Array.isArray(docsQ.data) && docsQ.data.length ? docsQ.data : FALLBACK_TOPICS

  return h(
    'div',
    { className: 'flex flex-col gap-4' },
    h(
      Section,
      { title: 'Host types' },
      h(
        'div',
        { className: 'grid gap-3 sm:grid-cols-2' },
        types.map(t =>
          h(
            'div',
            { key: t.kind, className: cx(CARD, isComingLater(t) ? 'opacity-70' : '') },
            h('div', { className: 'flex items-center justify-between gap-2' }, h('span', { className: 'text-sm font-semibold' }, t.title), isComingLater(t) ? h(Badge, { variant: 'outline' }, 'coming later') : null),
            h('div', { className: cx(SMALL, SECONDARY) }, t.one_liner),
            t.when_to_use ? h('div', { className: cx(SMALL, MUTED) }, 'When to use: ' + t.when_to_use) : null,
            h(CapChips, { capabilities: t.capabilities, notes: capNotes(t) }),
            t.learn_more_topic ? h('div', {}, h(Button, { size: 'xs', variant: 'outline', onClick: () => setOpen({ topic: t.learn_more_topic, title: t.title }) }, 'Learn more')) : null
          )
        )
      )
    ),
    h(
      Section,
      { title: 'Own Compute guide' },
      h('div', { className: cx(SMALL, MUTED) }, 'Help topics for this plugin. Select one to read it.'),
      h(
        'ul',
        { className: 'flex flex-col gap-1.5' },
        topics.map(tp =>
          h(
            'li',
            { key: tp.topic },
            h(
              'button',
              {
                type: 'button',
                onClick: () => setOpen({ topic: tp.topic, title: tp.title }),
                className: cx('flex w-full items-center gap-3 rounded-md border border-(--ui-stroke-secondary) px-2.5 py-1.5 text-left hover:border-(--ui-accent)', SMALL)
              },
              h('span', { className: 'font-medium' }, tp.title || tp.topic),
              h('span', { className: MUTED }, 'Read \u203a')
            )
          )
        )
      )
    ),
    open ? h(DocDialog, { key: open.topic, topic: open.topic, title: open.title, onClose: () => setOpen(null) }) : null
  )
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

function Shell({ children }) {
  return h(
    'div',
    { className: 'flex h-full min-h-0 flex-col gap-3 overflow-y-auto p-4 text-sm' },
    h('h1', { className: 'text-base font-semibold' }, 'Own Compute'),
    children
  )
}

function MissingBackend({ q }) {
  return h(
    ErrorState,
    { title: 'The Own Compute backend is not running', description: MISSING_BACKEND },
    h('div', { className: 'mb-2 text-center text-xs text-(--ui-text-secondary)' }, MISSING_BACKEND_MORE),
    h(
      'div',
      { className: 'flex justify-center gap-2' },
      h(CopyButton, { text: 'hermes plugins enable cloud-compute', label: 'Copy command', size: 'sm' }),
      h(Button, { variant: 'outline', size: 'sm', onClick: () => invalidateAll() }, 'Retry')
    )
  )
}

// Shared /health gate: null when the page can render, otherwise the element to
// show (loading, missing backend on 404/403, or a Retry error).
function healthGate(health) {
  const state = qState(health)
  if (state === 'loading') return h(Loading, {})
  if (state !== 'error') return null
  const info = errInfo(health.error)
  if (info.status === 404 || info.status === 403) return h(MissingBackend, { q: health })
  return h(ErrorState, { title: 'Own Compute is not responding', description: info.message }, h(Button, { variant: 'outline', size: 'sm', onClick: () => invalidateAll() }, 'Retry'))
}

function FirstRun({ onSaving, onSaveFailed, onSaved, onFinished }) {
  return h(
    'div',
    { className: 'mx-auto flex w-full max-w-2xl flex-col gap-4' },
    h(EmptyState, {
      title: 'Connect your first machine',
      description: 'Pick a type, check the connection, then set limits for the environments it will run. Need background first? Open the Help tab.'
    }),
    h(AddHostWizard, {
      embedded: true,
      onSaving,
      onSaveFailed,
      onSaved,
      onFinished: r => {
        if (onFinished) onFinished(r)
      }
    })
  )
}

function readSavedTab() {
  try {
    const saved = ctx && ctx.storage ? ctx.storage.get('tab', 'overview') : 'overview'
    return TABS.some(t => t.id === saved) ? saved : 'overview'
  } catch (_) {
    return 'overview'
  }
}

// Hermes' own setting approvals.mode: off lets chat requests run without an approval card.
// The plugin does not change which actions ask; the page just says when no card will come.
const APPROVALS_OFF_NOTICE =
  'Approvals are switched off in Hermes settings. Chat requests to start, change or delete things on your machines run without asking first. Turn approvals on in Settings to see a card before each one.'

function approvalsOff(summary) {
  return !!(summary && summary.approvals && summary.approvals.off === true)
}

function ApprovalsNotice() {
  const key = useQueryKey()
  const q = useQuery({ queryKey: key('summary'), queryFn: () => api('GET', '/summary'), refetchInterval: POLL_MS, retry: false })
  if (!approvalsOff(q.data)) return null
  return h('div', { role: 'alert', className: cx('rounded-md border border-(--ui-stroke-secondary) p-2 font-medium text-destructive', SMALL) }, APPROVALS_OFF_NOTICE)
}

function Page() {
  const key = useQueryKey()
  const intent = useValue($intent)
  const [tab, setTab] = useState(readSavedTab)
  const [firstTab, setFirstTab] = useState('start')
  // The first-run wizard stays mounted from the moment Save is pressed until
  // Done, even after /health flips to configured (the backend event can land
  // before the POST reply). `entered` leaves it for the tabs.
  const [pinned, setPinned] = useState(false)
  const [entered, setEntered] = useState(false)
  // Follows the backend: a data reset or a first host shows up without a reload.
  const health = useQuery({ queryKey: key('health'), queryFn: () => api('GET', '/health'), refetchInterval: HEALTH_POLL_MS, retry: false })
  const configured = health.data ? health.data.configured !== false : null

  useEffect(() => {
    if (intent && intent.tab && TABS.some(t => t.id === intent.tab)) setTab(intent.tab)
  }, [intent])

  // `entered` only bridges a stale /health between Done and the next refetch:
  // once /health says configured it has done its job, so a later data reset
  // (configured -> false) falls back to the first-run view.
  useEffect(() => {
    if (configured === true && entered) setEntered(false)
  }, [configured, entered])

  // A reset empties the backend: drop what the tabs cached so no stale list
  // (jobs, hosts, instances, agents) shows when they come back.
  const wasConfigured = useRef(configured)

  useEffect(() => {
    if (configured === false && wasConfigured.current === true) {
      queryClient.removeQueries({ queryKey: ['cloud-compute'], predicate: q => q.queryKey[3] !== 'health' })
    }

    wasConfigured.current = configured
  }, [configured])

  const changeTab = next => {
    setTab(next)
    try {
      if (ctx && ctx.storage) ctx.storage.set('tab', next)
    } catch (_) {
      // Remembering the tab is optional.
    }
  }

  const gate = healthGate(health)
  if (gate) return h(Shell, {}, gate)

  if (pinned || (configured === false && !entered)) {
    // Before any host exists: the wizard, plus Help so the page can be read first.
    return h(
      Shell,
      {},
      h(Tabs, { value: firstTab, onValueChange: setFirstTab }, h(TabsList, {}, FIRST_RUN_TABS.map(t => h(TabsTrigger, { key: t.id, value: t.id }, t.label)))),
      h(
        'div',
        { className: firstTab === 'start' ? '' : 'hidden' },
        h(FirstRun, {
          onSaving: () => setPinned(true),
          onSaveFailed: () => setPinned(false),
          onSaved: () => setPinned(true),
          onFinished: () => {
            setPinned(false)
            setEntered(true)
          }
        })
      ),
      firstTab === 'help' ? h(HelpTab, {}) : null
    )
  }

  let body = null
  if (tab === 'hosts') body = h(HostsTab, {})
  else if (tab === 'templates') body = h(TemplatesTab, { goTab: changeTab })
  else if (tab === 'jobs') body = h(JobsTab, {})
  else if (tab === 'agents') body = h(AgentsTab, {})
  else if (tab === 'help') body = h(HelpTab, {})
  else body = h(OverviewTab, {})

  return h(
    Shell,
    {},
    h(ApprovalsNotice, {}),
    h(Tabs, { value: tab, onValueChange: changeTab }, h(TabsList, {}, TABS.map(t => h(TabsTrigger, { key: t.id, value: t.id }, t.label)))),
    body
  )
}

// ---------------------------------------------------------------------------
// Chat card (::cc-env{id="i-xxxxxxxx"})
// ---------------------------------------------------------------------------

// The model writes the attributes, so only a well-formed instance id is used.
// Anything else renders an inert line and never reaches the backend.
function envIdFrom(attrs) {
  if (!attrs || typeof attrs !== 'object' || !Object.prototype.hasOwnProperty.call(attrs, 'id')) return null
  const id = attrs.id
  return typeof id === 'string' && INSTANCE_ID_RE.test(id) ? id : null
}

function ChatCard(props) {
  const p = props || {}
  const id = envIdFrom(p.attrs || { id: p.id })
  const streaming = !!p.streaming
  const key = useQueryKey()
  const act = useAction()
  const term = useTerminalCooldown()
  const q = useQuery({
    queryKey: key('instance', id || 'none'),
    queryFn: () => api('GET', '/instances/' + id),
    enabled: !!id && !streaming,
    retry: false
  })
  // A deleted environment shows where its files were saved.
  const gone = !!q.data && q.data.status === 'destroyed'
  const copyQ = useQuery({
    queryKey: key('card-exports', id || 'none'),
    queryFn: () => api('GET', '/exports?instance_id=' + id),
    enabled: !!id && !streaming && gone,
    retry: false
  })

  const line = text => h('div', { className: cx('rounded-md border border-(--ui-stroke-secondary) px-2.5 py-1.5', SMALL, MUTED) }, text)
  if (!id) return line('Unknown environment')
  if (streaming) return line('Environment\u2026')
  const state = qState(q)
  if (state === 'loading') return line('Loading environment\u2026')
  if (state === 'error') return line(errInfo(q.error).status === 404 ? 'Environment ' + id + ' was not found.' : "Couldn't load environment " + id + '.')

  const inst = q.data || {}
  const copies = copyQ.data && Array.isArray(copyQ.data.exports) ? copyQ.data.exports : []
  const wanted = instanceActions(inst).filter(a => a === 'terminal' || a === 'screen')
  const tone = inst.status === 'running' ? 'good' : inst.status === 'creating' ? 'warn' : inst.status === 'error' ? 'bad' : 'muted'
  const buttons = {
    terminal: h(Button, { key: 'terminal', size: 'xs', variant: 'outline', disabled: act.busy || term.cooling, onClick: () => { if (term.cooling) return undefined; term.start(); return act.run(() => openTerminal({ kind: 'instance', id: inst.id || id, name: inst.name || id })) } }, 'Terminal'),
    screen: h(Button, { key: 'screen', size: 'xs', variant: 'outline', disabled: act.busy, onClick: () => act.run(() => openScreen(Object.assign({}, inst, { id: inst.id || id }))) }, 'Screen')
  }
  return h(
    'div',
    { className: CARD },
    h(
      'div',
      { className: 'flex flex-wrap items-center justify-between gap-2' },
      h('span', { className: 'flex min-w-0 items-center gap-2' }, h(StatusDot, { tone }), h('span', { className: 'truncate text-sm font-semibold' }, inst.name || id), h(Badge, { variant: inst.status === 'error' ? 'destructive' : 'muted' }, inst.status || 'unknown')),
      h('span', { className: cx(SMALL, MUTED) }, 'Host ' + (inst.host_id || '?') + ' \u00b7 Template ' + (inst.template_id || '?'))
    ),
    h(ErrorLine, { message: act.error }),
    h(ScreenFix, { code: act.code }),
    inst.status === 'destroyed'
      ? h(
          'div',
          { className: cx('flex flex-col gap-1', SMALL, SECONDARY) },
          h('span', {}, 'This environment was deleted.'),
          copies[0]
            ? h(Fragment, {}, h('span', {}, 'Its files were saved: ' + (copies[0].files > 0 ? sizeText(copies[0].bytes, copies[0].files) : fmtBytes(copies[0].bytes)) + ' from ' + (copies[0].source_path || DEFAULT_WORKDIR) + '.'), h(PathLine, { path: copies[0].local_path }))
            : copyQ.data !== undefined
              ? h('span', { className: MUTED }, 'No saved copy exists for it.')
              : null
        )
      : null,
    wanted.length ? h('div', { className: 'flex flex-wrap gap-1.5' }, wanted.map(a => buttons[a])) : null
  )
}

// ---- Chat cards: job, agent run, saved copy, host -----------------------------------
// The tools put a `card` line such as ::cc-job{id="j-1a2b3c4d"} in their results; the model
// pastes it into the reply and Desktop draws one of these. Each card reads live data from
// the same REST routes as the page and says plainly when the thing no longer exists.

const CARD_KINDS = {
  job: { directive: 'cc-job', noun: 'job', pattern: /^j-[0-9a-f]{8}$/, path: id => '/jobs/' + id, gone: 'It was removed from the list.' },
  agent: { directive: 'cc-agent', noun: 'cloud agent run', pattern: /^a-[0-9a-f]{8}$/, path: id => '/agents/' + id, gone: 'Its record was removed.' },
  copy: { directive: 'cc-copy', noun: 'saved copy', pattern: /^x-[0-9a-f]{8}$/, path: id => '/exports/' + id, gone: 'It was deleted from this computer.' },
  host: { directive: 'cc-host', noun: 'host', pattern: /^[a-z0-9-]{1,32}$/, path: id => '/hosts/' + id, gone: 'It was removed.' }
}

function cardIdFrom(attrs, kind) {
  if (!attrs || typeof attrs !== 'object' || !Object.prototype.hasOwnProperty.call(attrs, 'id')) return null
  const id = attrs.id
  return typeof id === 'string' && CARD_KINDS[kind].pattern.test(id) ? id : null
}

const CARD_LINE = 'rounded-md border border-(--ui-stroke-secondary) px-2.5 py-1.5'

// The directive's data and the line to show while there is no data to draw yet.
function useDirectiveData(kind, props) {
  const p = props || {}
  const spec = CARD_KINDS[kind]
  const id = cardIdFrom(p.attrs || { id: p.id }, kind)
  const streaming = !!p.streaming
  const key = useQueryKey()
  const q = useQuery({
    queryKey: key('card', kind, id || 'none'),
    queryFn: () => api('GET', spec.path(id)),
    enabled: !!id && !streaming,
    retry: false
  })
  const line = text => h('div', { className: cx(CARD_LINE, SMALL, MUTED) }, text)
  let wait = null
  if (!id) wait = line('Unknown ' + spec.noun)
  else if (streaming) wait = line(spec.noun.charAt(0).toUpperCase() + spec.noun.slice(1) + '\u2026')
  else {
    const state = qState(q)
    if (state === 'loading') wait = line('Loading ' + spec.noun + '\u2026')
    else if (state === 'error') {
      const info = errInfo(q.error)
      wait = line(info.status === 404 ? 'The ' + spec.noun + ' ' + id + ' no longer exists. ' + spec.gone : "Couldn't load the " + spec.noun + ' ' + id + '.')
    }
  }
  return { id, data: q.data, wait }
}

function JobChatCard(props) {
  const { id, data, wait } = useDirectiveData('job', props)
  const act = useAction()
  const [dialog, setDialog] = useState(null)
  if (wait) return wait
  const job = data || {}
  const live = JOB_CANCELLABLE.indexOf(job.state) >= 0
  const facts = [job.host_id ? 'Host ' + job.host_id : null, job.partition ? 'Partition ' + job.partition : null, job.nodes ? 'Nodes ' + job.nodes : null, jobStartFact(job), job.ended_at ? 'Ended ' + ago(job.ended_at) : null, job.exit_code ? 'Exit code ' + job.exit_code : null].filter(Boolean)
  const post = (method, path, done) =>
    act.run(async () => {
      const res = await api(method, path)
      toast('success', done(res))
      invalidateAll()
    })
  return h(
    'div',
    { className: CARD },
    h(
      'div',
      { className: 'flex flex-wrap items-center justify-between gap-2' },
      h('span', { className: 'flex min-w-0 items-center gap-2' }, h(StatusDot, { tone: jobTone(job.state) }), h('span', { className: 'truncate text-sm font-semibold' }, job.name || id), h(Badge, { variant: JOB_FAILED.indexOf(job.state) >= 0 ? 'destructive' : 'muted' }, jobStateLabel(job))),
      h('span', { className: cx(SMALL, MUTED) }, job.slurm_job_id ? 'Slurm job ' + job.slurm_job_id : id)
    ),
    facts.length ? h('div', { className: cx('flex flex-wrap gap-x-4 gap-y-0.5', SMALL, SECONDARY) }, facts.map(f => h('span', { key: f }, f))) : null,
    h(ErrorLine, { message: act.error }),
    h(
      'div',
      { className: 'flex flex-wrap gap-1.5' },
      h(Button, { size: 'xs', variant: 'outline', onClick: () => setDialog('output') }, 'Output'),
      live ? h(Button, { size: 'xs', variant: 'ghost', onClick: () => setDialog('cancel') }, 'Cancel') : null,
      jobFinished(job) ? h(Button, { size: 'xs', variant: 'ghost', onClick: () => setDialog('delete') }, 'Remove from list') : null
    ),
    dialog === 'output' ? h(JobOutputDialog, { job: Object.assign({ id }, job), onClose: () => setDialog(null) }) : null,
    dialog === 'cancel'
      ? h(ConfirmDialog, {
          open: true,
          destructive: true,
          title: 'Cancel ' + (job.name || id) + '?',
          description: 'Slurm stops the job. Output written so far stays available.',
          confirmLabel: 'Cancel job',
          onClose: () => setDialog(null),
          onConfirm: async () => {
            try {
              await api('POST', '/jobs/' + encodeURIComponent(id) + '/cancel')
            } catch (e) {
              throw new Error(errInfo(e).message)
            }
            toast('success', 'Cancelled ' + (job.name || id) + '.')
            invalidateAll()
          }
        })
      : null,
    dialog === 'delete'
      ? h(ConfirmDialog, {
          open: true,
          destructive: true,
          title: 'Remove ' + (job.name || id) + ' from the list?',
          description: jobRemoveText(job),
          confirmLabel: 'Remove from list',
          onClose: () => setDialog(null),
          onConfirm: async () => {
            let res
            try {
              res = await api('DELETE', '/jobs/' + encodeURIComponent(id))
            } catch (e) {
              throw new Error(errInfo(e).message)
            }
            toast('success', (res && res.note) || 'Removed ' + (job.name || id) + ' from the list.')
            invalidateAll()
          }
        })
      : null
  )
}

function AgentChatCard(props) {
  const { id, data, wait } = useDirectiveData('agent', props)
  const act = useAction()
  const [dialog, setDialog] = useState(null)
  const [result, setResult] = useState(null)
  if (wait) return wait
  const run = data || {}
  const live = AGENT_ACTIVE.indexOf(run.status) >= 0
  const canFetch = AGENT_COLLECTABLE.indexOf(run.status) >= 0 && (run.commits_ahead || 0) > 0 && !result
  const facts = [run.host_id ? 'Host ' + run.host_id : null, run.branch ? 'Branch ' + run.branch : null, Number.isFinite(run.commits_ahead) ? run.commits_ahead + ' ' + plural(run.commits_ahead, 'commit', 'commits') + ' ahead' : null].concat(agentFacts(run)).filter(Boolean)
  const collect = () =>
    act.run(async () => {
      const res = await api('POST', '/agents/' + encodeURIComponent(id) + '/collect', { into: 'worktree' })
      setResult(res)
      toast(res && res.diverged ? 'warning' : 'success', res && res.diverged ? id + ': collected, but your branch has diverged; the version of the agent was kept separately.' : 'Collected ' + id + ' into ' + ((res && res.ref) || 'your repository') + '.')
      invalidateAll()
    })
  return h(
    'div',
    { className: CARD },
    h(
      'div',
      { className: 'flex flex-wrap items-center justify-between gap-2' },
      h('span', { className: 'flex min-w-0 items-center gap-2' }, h(StatusDot, { tone: agentTone(run.status) }), h('span', { className: 'truncate text-sm font-semibold' }, id), h(Badge, { variant: agentBadge(run.status) }, run.status || 'unknown'), run.mode === 'script' ? h(Badge, { variant: 'outline' }, 'handoff test') : null),
      h('span', { className: cx(SMALL, MUTED) }, run.template_id || '')
    ),
    run.goal ? h('div', { className: cx('line-clamp-3 whitespace-pre-wrap', SMALL) }, run.goal) : null,
    facts.length ? h('div', { className: cx('flex flex-wrap gap-x-4 gap-y-0.5', SMALL, SECONDARY) }, facts.map(f => h('span', { key: f }, f))) : null,
    h(AgentFailure, { run }),
    h(AgentReply, { run }),
    result ? h('div', { className: cx(SMALL, SECONDARY) }, 'Results fetched: branch ' + (result.ref || '?') + (result.worktree_path ? ', worktree ' + result.worktree_path : '') + '. Nothing was merged.') : null,
    h(ErrorLine, { message: act.error }),
    h(
      'div',
      { className: 'flex flex-wrap gap-1.5' },
      canFetch ? h(Button, { size: 'xs', disabled: act.busy, onClick: collect }, act.busy ? 'Working\u2026' : 'Get results') : null,
      !live && run.status !== 'stopped' ? h(Button, { size: 'xs', variant: 'outline', onClick: () => setDialog('message') }, 'Send a message\u2026') : null,
      h(
        Button,
        {
          size: 'xs',
          variant: 'ghost',
          onClick: () => {
            setIntent('agents')
            openPage()
          }
        },
        'Open in Compute'
      )
    ),
    dialog === 'message'
      ? h(Modal, { open: true, onClose: () => setDialog(null), title: 'Message to ' + id, description: 'The agent continues in the same sandbox and branch.' }, h(FollowUpBox, { run }))
      : null
  )
}

function CopyChatCard(props) {
  const { id, data, wait } = useDirectiveData('copy', props)
  const [restoring, setRestoring] = useState(false)
  if (wait) return wait
  const item = data || {}
  const src = item.source_path || DEFAULT_WORKDIR
  return h(
    'div',
    { className: CARD },
    h(
      'div',
      { className: 'flex flex-wrap items-center justify-between gap-2' },
      h('span', { className: 'flex min-w-0 items-center gap-2' }, h(StatusDot, { tone: 'good' }), h('span', { className: 'truncate text-sm font-semibold' }, 'Saved copy of ' + (item.instance_name || item.instance_id || id)), h(Badge, { variant: 'muted' }, EXPORT_REASONS[item.reason] || item.reason || 'saved')),
      h('span', { className: cx(SMALL, MUTED) }, item.created_at ? 'Saved ' + ago(item.created_at) : id)
    ),
    h('div', { className: cx(SMALL, SECONDARY) }, (item.files > 0 ? sizeText(item.bytes, item.files) : fmtBytes(item.bytes)) + ' from ' + src),
    item.local_path ? h(PathLine, { path: item.local_path }) : null,
    item.extracted_to ? h('div', { className: cx('flex flex-col gap-1', SMALL, MUTED) }, h('span', {}, 'Also unpacked into'), h(PathLine, { path: item.extracted_to })) : null,
    h('div', { className: 'flex flex-wrap gap-1.5' }, h(Button, { size: 'xs', variant: 'outline', onClick: () => setRestoring(true) }, 'Restore into\u2026')),
    restoring ? h(RestoreDialog, { item: Object.assign({ id }, item), current: null, onClose: () => setRestoring(false) }) : null
  )
}

function HostChatCard(props) {
  const { id, data, wait } = useDirectiveData('host', props)
  const act = useAction()
  const shell = useTerminalCooldown()
  if (wait) return wait
  const spec = (data && data.spec) || {}
  const state = (data && data.state) || null
  const look = hostLook(spec, state)
  const status = look.status
  const light = look.light
  const res = look.calm ? null : state && state.resources
  const meters = res ? hostMeters(res).filter(m => Number.isFinite(m.total)).map(m => m.label + ' ' + (Number.isFinite(m.used) ? m.fmt(m.used) + ' / ' : '') + m.fmt(m.total)) : []
  return h(
    'div',
    { className: CARD },
    h(
      'div',
      { className: 'flex flex-wrap items-center justify-between gap-2' },
      h('span', { className: 'flex min-w-0 items-center gap-2' }, h(LightDot, { status }), h('span', { className: 'truncate text-sm font-semibold' }, spec.name || id), h(Badge, { variant: 'outline' }, typeTitle(spec.kind)), h('span', { className: cx(SMALL, SECONDARY) }, light.label)),
      h('span', { className: cx(SMALL, MUTED) }, (spec.user ? spec.user + '@' : '') + hostAddress(spec))
    ),
    meters.length ? h('div', { className: cx('flex flex-wrap gap-x-4 gap-y-0.5', SMALL, SECONDARY) }, meters.map(m => h('span', { key: m }, m))) : null,
    state && state.last_error && !look.calm ? h('div', { className: cx('text-destructive', SMALL) }, 'Last error: ' + state.last_error) : null,
    light.action && status !== 'online' ? h('div', { className: cx(SMALL, MUTED) }, 'What to do: ' + light.action) : null,
    capsNote(state),
    h(ErrorLine, { message: act.error }),
    h(
      'div',
      { className: 'flex flex-wrap gap-1.5' },
      h(
        Button,
        {
          size: 'xs',
          variant: 'outline',
          disabled: act.busy,
          onClick: () =>
            act.run(async () => {
              await api('POST', '/hosts/' + encodeURIComponent(id) + '/probe')
              invalidateAll()
            })
        },
        act.busy ? 'Checking\u2026' : 'Refresh status'
      ),
      spec.kind === 'slurm' ? h(Button, { size: 'xs', variant: 'outline', disabled: act.busy || shell.cooling, onClick: () => { if (shell.cooling) return undefined; shell.start(); return act.run(() => openTerminal({ kind: 'host', id, name: spec.name || id })) } }, 'Open shell') : null,
      h(
        Button,
        {
          size: 'xs',
          variant: 'ghost',
          onClick: () => {
            setIntent('hosts')
            openPage()
          }
        },
        'Open Hosts'
      )
    )
  )
}

// The failed work-directory check of a Slurm host, in the same words as the host card.
function capsNote(state) {
  const caps = (state && state.capabilities) || {}
  return caps.workdir_shared === false && caps.workdir_detail ? h('div', { className: cx('text-destructive', SMALL) }, 'Work directory check failed: ' + caps.workdir_detail) : null
}

// ---------------------------------------------------------------------------
// Status chip and settings page
// ---------------------------------------------------------------------------

function StatusChip() {
  const key = useQueryKey()
  const q = useQuery({ queryKey: key('summary'), queryFn: () => api('GET', '/summary'), retry: false })
  const chipState = qState(q)
  // Nothing while loading (no flash of a dead item) or when the backend is missing.
  if (chipState === 'loading') return null
  if (chipState === 'error') {
    const info = errInfo(q.error)
    if (info.status === 404 || info.status === 403) return null
  }
  const model = q.data ? chipModel(q.data) : { text: 'Compute', tone: 'idle' }
  const attention = q.data && Array.isArray(q.data.attention) ? q.data.attention.map(a => a.message).filter(Boolean) : []
  const tip = attention.length ? attention.join('\n') : 'Open Own Compute'
  return h(
    Tip,
    { label: tip },
    h(
      'button',
      {
        type: 'button',
        onClick: () => host.navigate(ROUTE),
        className: cx('inline-flex h-full items-center gap-1.5 px-1.5 text-[0.6875rem] hover:text-(--ui-text-primary)', SECONDARY)
      },
      h(StatusDot, { tone: chipTone(model.tone) }),
      h('span', {}, model.text)
    )
  )
}

const HOST_STATUS_ORDER = ['online', 'degraded', 'auth_required', 'offline', 'unknown']

function hostCounts(list) {
  const counts = {}
  list.forEach(i => {
    const look = hostLook(i && i.spec, i && i.state)
    const status = look.calm ? 'idle' : look.status
    counts[status] = (counts[status] || 0) + 1
  })
  return counts
}

// 'idle' is an EC2 host without a running machine: a neutral dot and its own words.
function overviewLight(status) {
  return status === 'idle' ? { status: 'unknown', label: 'no machine running' } : { status, label: statusLight(status).label.toLowerCase() }
}

function SettingsPage() {
  const key = useQueryKey()
  const health = useQuery({ queryKey: key('health'), queryFn: () => api('GET', '/health'), retry: false })
  const q = useQuery({ queryKey: key('hosts'), queryFn: () => api('GET', '/hosts'), retry: false, enabled: qState(health) === 'ready' })
  const gate = healthGate(health)
  return h(
    'div',
    { className: 'flex flex-col gap-4 p-4 text-sm' },
    h('div', { className: cx(SMALL, SECONDARY) }, 'Own Compute runs sandboxed environments and batch jobs on machines you own or have an account on.'),
    h(
      Section,
      { title: 'Hosts' },
      gate ? gate : h(Async, { q, label: 'Loading hosts\u2026' }, data => {
        const list = Array.isArray(data) ? data : []
        if (!list.length) return h('div', { className: cx(SMALL, MUTED) }, 'No hosts yet.')
        const counts = hostCounts(list)
        const order = HOST_STATUS_ORDER.filter(s => counts[s]).concat(Object.keys(counts).filter(s => HOST_STATUS_ORDER.indexOf(s) < 0))
        return h(
          'div',
          { className: 'flex flex-col gap-2' },
          h('div', { className: SMALL }, list.length + ' ' + plural(list.length, 'host', 'hosts')),
          h(
            'ul',
            { className: 'flex flex-wrap gap-3' },
            order.map(status =>
              h('li', { key: status, className: cx('flex items-center gap-1.5', SMALL) }, h(LightDot, { status: overviewLight(status).status }), h('span', {}, counts[status] + ' ' + overviewLight(status).label))
            )
          ),
          h(
            'ul',
            { className: 'flex flex-col gap-1' },
            list.map(i =>
              h('li', { key: i.spec.id, className: cx('flex items-center gap-2', SMALL) }, h(LightDot, { status: hostLook(i.spec, i.state).status }), h('span', {}, i.spec.name || i.spec.id), h('span', { className: MUTED }, hostLook(i.spec, i.state).light.label))
            )
          )
        )
      })
    ),
    h('div', {}, h(Button, { size: 'sm', onClick: () => host.navigate(ROUTE) }, 'Open Compute')),
    h(
      Section,
      { title: 'Agent settings' },
      h(
        'div',
        { className: cx(SMALL, SECONDARY) },
        'Limits and defaults for the tools the agent uses are in the Agent settings sub-page of this entry in Settings, under Plugins.'
      ),
      h('div', {}, h(Button, { size: 'sm', variant: 'outline', onClick: () => host.navigate(pluginSettingsHref(PLUGIN_ID)) }, 'Show plugin settings'))
    )
  )
}

// ---------------------------------------------------------------------------
// Events and notifications
// ---------------------------------------------------------------------------

function openPage() {
  host.navigate(ROUTE)
}

function osNotify(title, body) {
  try {
    if (ctx && ctx.os && typeof ctx.os.notify === 'function') ctx.os.notify({ title, body })
  } catch (_) {
    // Native notifications are best effort.
  }
}

function jobFinishedKind(state) {
  if (state === 'COMPLETED') return 'success'
  if (JOB_FAILED.indexOf(state) >= 0) return 'error'
  return 'warning'
}

// Payloads carry names, ids and states only; nothing else is shown.
function handleEvent(type, event) {
  const payload = event && event.payload
  if (shouldInvalidate(type, payload)) invalidateAll()
  if (type === EVENT_PREFIX + 'instances.changed' || type === EVENT_PREFIX + 'tunnels.changed') {
    if (openScreens.size) checkScreens()
  }
  if (!payload || type.indexOf(EVENT_PREFIX) !== 0) return
  const name = type.slice(EVENT_PREFIX.length)
  const open = { label: 'Open', onClick: openPage }
  if (name === 'job.finished') {
    const state = String(payload.state || 'UNKNOWN')
    const unconfirmed = payload.confirmed === false
    const who = String(payload.name || payload.id || 'Job')
    const msg = unconfirmed ? who + ' ended: the final state could not be confirmed' : who + ' finished: ' + state
    toast(unconfirmed ? 'warning' : jobFinishedKind(state), msg, { id: 'cc-job-' + payload.id, action: open })
    osNotify('Slurm job finished', msg)
  } else if (name === 'host.status' && payload.status === 'offline' && payload.prev !== 'offline' && !payload.expected) {
    const msg = 'Host ' + payload.id + ' went offline.'
    toast('warning', msg, { id: 'cc-host-' + payload.id, action: open })
    osNotify('Host offline', msg)
  } else if (name === 'instance.expiring') {
    const left = Number(payload.minutes_left)
    const who = String(payload.name || payload.id || 'An environment')
    const msg = who + ' reaches its time limit ' + (Number.isFinite(left) ? 'in ' + left + ' ' + plural(left, 'minute', 'minutes') : 'soon') + ' and will be stopped. Its files are kept.'
    toast('warning', msg, {
      id: 'cc-expiring-' + payload.id,
      action: {
        label: 'Extend',
        onClick: () => {
          setIntent('overview', 'extend', { instanceId: payload.id })
          openPage()
        }
      }
    })
    osNotify('Time limit reached soon', msg)
  } else if (name === 'instances.changed' && payload.reason === 'error') {
    toast('error', 'An environment on ' + (payload.host_id || 'a host') + ' hit an error.', { id: 'cc-inst-error', action: open })
  } else if (name === 'notify') {
    const level = payload.level
    const kind = level === 'error' ? 'error' : level === 'warn' || level === 'warning' ? 'warning' : 'info'
    toast(kind, String(payload.body || payload.title || 'Own Compute'), { title: payload.title || undefined, action: open })
  }
}

// ---------------------------------------------------------------------------
// Plugin entry
// ---------------------------------------------------------------------------

export default {
  id: PLUGIN_ID,
  name: 'Own Compute',
  description: 'Bring your own compute: Hermes sandboxes, Slurm jobs and agents on the machines and cluster accounts you already have. Shows a live status chip.',
  defaultEnabled: false,
  register(ctx) {
    setCtx(ctx)

    ctx.register({
      id: 'nav',
      area: SIDEBAR_NAV_AREA,
      order: 60,
      data: { path: ROUTE, label: 'Compute', codicon: 'server-environment' }
    })
    ctx.register({
      id: 'page',
      area: ROUTES_AREA,
      title: 'Compute',
      data: { path: ROUTE },
      render: () => jsx(Page, {})
    })
    ctx.register({
      id: 'chip',
      area: STATUSBAR_AREAS.right,
      order: 100,
      render: () => jsx(StatusChip, {})
    })

    const palette = (id, label, keywords, run) =>
      ctx.register({
        id,
        area: PALETTE_AREA,
        data: { id: PLUGIN_ID + '.' + id, label, keywords, run }
      })
    palette('open', 'Open Compute', ['cloud', 'compute', 'environments', 'hosts'], () => openPage())
    palette('add-host', 'Add host', ['cloud', 'compute', 'ssh', 'slurm', 'machine'], () => {
      setIntent('hosts', 'add-host')
      openPage()
    })
    palette('spawn', 'Spawn environment\u2026', ['cloud', 'compute', 'start', 'container', 'sandbox'], () => {
      setIntent('templates', 'spawn')
      openPage()
    })
    palette('submit-job', 'Submit Slurm job\u2026', ['cloud', 'compute', 'slurm', 'batch', 'sbatch'], () => {
      setIntent('jobs', 'submit-job')
      openPage()
    })

    palette('new-agent', 'New cloud agent run\u2026', ['cloud', 'compute', 'agent', 'hermes', 'branch', 'git'], () => {
      setIntent('agents', 'new-agent')
      openPage()
    })

    if (typeof ctx.registerSettingsPage === 'function') {
      ctx.registerSettingsPage({
        id: PLUGIN_ID,
        title: 'Own Compute',
        render: () => jsx(SettingsPage, {})
      })
    }

    ctx.register({
      id: 'cc-env-card',
      area: TRANSCRIPT_DIRECTIVE_AREA,
      data: {
        name: 'cc-env',
        render: ({ attrs, sessionId, streaming }) => jsx(ChatCard, { attrs, sessionId, streaming })
      }
    })

    const cardComponents = { job: JobChatCard, agent: AgentChatCard, copy: CopyChatCard, host: HostChatCard }
    Object.keys(CARD_KINDS).forEach(kind => {
      const Card = cardComponents[kind]
      ctx.register({
        id: CARD_KINDS[kind].directive + '-card',
        area: TRANSCRIPT_DIRECTIVE_AREA,
        data: {
          name: CARD_KINDS[kind].directive,
          render: ({ attrs, sessionId, streaming }) => jsx(Card, { attrs, sessionId, streaming })
        }
      })
    })

    EVENT_NAMES.forEach(name => {
      const type = EVENT_PREFIX + name
      ctx.onEvent(type, event => handleEvent(type, event))
    })
    ctx.onEvent('tool.complete', event => handleEvent('tool.complete', event))

    ctx.setInterval(() => {
      queryClient.invalidateQueries({ queryKey: queryKey('summary') })
    }, CHIP_POLL_MS)
  }
}

export const __test__ = {
  API_CALLS,
  replyHasBoxLines,
  browserIsRemote,
  portOfAddress,
  validatePorts,
  publishedPorts,
  STEP_LABELS,
  instanceActions,
  jobActions,
  jobStateLabel,
  jobUnconfirmed,
  hostMeters,
  buildJobRequest,
  openTerminal,
  resetTerminalGuard,
  groupHosts,
  capNotes,
  DOC_CSS,
  openScreen,
  chipModel,
  shouldInvalidate,
  queryKey,
  maskUrl,
  maskText,
  stopReasonText,
  timeLimitInfo,
  startNeedsNotice,
  fmtBytes,
  fileErrorText,
  pathCrumbs,
  handleEvent,
  isAgentTemplate,
  templateInfo,
  sameTemplate,
  secretsToSend,
  $deleted,
  agentActions,
  agentFacts,
  agentFailedBeforeWork,
  jobStartFact,
  modelWillUse,
  modelIsDefault,
  specialPathError,
  workdirFailure,
  eligibleHosts,
  spawnMachineNote,
  deletedText,
  stoppedText,
  approvalsOff,
  APPROVALS_OFF_NOTICE,
  agentSandboxGone,
  buildAgentRequest,
  cleanPath,
  fileFacts,
  splitInstances,
  partitionLoad,
  agentResults,
  agentTone,
  agentBadge,
  draftFromRun,
  $agentDraft,
  openScreens,
  checkScreens,
  COPY,
  components: {
    Page,
    OverviewTab,
    HostsTab,
    TemplatesTab,
    JobsTab,
    AgentsTab,
    AgentRunCard,
    NewAgentDialog,
    TemplateEditor,
    TemplateCard,
    SpawnDialog,
    NewTemplateDialog,
    ForwardDialog,
    ExtendDialog,
    DeleteDialog,
    DeletedNotice,
    FilesDialog,
    SaveDialog,
    SaveResult,
    UploadPanel,
    RerunDialog,
    ApprovalsNotice,
    RestoreDialog,
    TunnelRow,
    HelpTab,
    StatusChip,
    SettingsPage,
    InstanceCard,
    HostCard,
    AddHostWizard,
    SubmitJobDialog,
    ScreenViewer,
    ChatCard,
    JobChatCard,
    AgentChatCard,
    CopyChatCard,
    HostChatCard
  }
}
