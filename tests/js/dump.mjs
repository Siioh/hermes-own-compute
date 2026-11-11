// Print the plugin's embedded UI copy and API call list as ONE JSON object.
//   node --import ./tests/js/register-stubs.mjs ./tests/js/dump.mjs
// {"copy": __test__.COPY, "api_calls": __test__.API_CALLS, "default": {id, name, defaultEnabled}}
import { pathToFileURL } from 'node:url'
import path from 'node:path'

const root = path.resolve(import.meta.dirname, '..', '..')
const pluginPath = process.env.CC_PLUGIN_PATH || path.join(root, 'desktop', 'plugin.js')
const mod = await import(pathToFileURL(pluginPath).href)
const plugin = mod.default
const api = mod.__test__

if (!api || typeof api !== 'object') {
  throw new Error('desktop/plugin.js has no __test__ export')
}

const out = {
  copy: api.COPY,
  api_calls: api.API_CALLS,
  default: { id: plugin?.id, name: plugin?.name, defaultEnabled: plugin?.defaultEnabled }
}

process.stdout.write(`${JSON.stringify(out)}\n`)
