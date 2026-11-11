// Module-resolution hooks for the desktop test harness.
// Maps the three specifiers the Desktop plugin loader allows onto local stubs
// and loads plain .js files as ES modules (no package.json needed).
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

const STUBS = new Map([
  ['@hermes/plugin-sdk', new URL('./stubs/sdk.mjs', import.meta.url).href],
  ['react', new URL('./stubs/react.mjs', import.meta.url).href],
  ['react/jsx-runtime', new URL('./stubs/jsx-runtime.mjs', import.meta.url).href]
])

export async function resolve(specifier, context, nextResolve) {
  const hit = STUBS.get(specifier)

  if (hit) {
    return { url: hit, format: 'module', shortCircuit: true }
  }

  return nextResolve(specifier, context)
}

export async function load(url, context, nextLoad) {
  if (url.startsWith('file:') && url.endsWith('.js') && !url.includes('/node_modules/')) {
    const source = await readFile(fileURLToPath(url))

    return { format: 'module', source, shortCircuit: true }
  }

  return nextLoad(url, context)
}
