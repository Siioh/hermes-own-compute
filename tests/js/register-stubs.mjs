// Usage: node --import ./tests/js/register-stubs.mjs ./tests/js/smoke.mjs
import { register } from 'node:module'

register('./hooks.mjs', import.meta.url)
