// Stub of `@hermes/plugin-sdk` for the desktop test harness.
//
// Exports EVERY runtime name exported by apps/desktop/src/sdk/index.ts of Hermes
// v0.23.0 (the list is in sdk-exports.json) so named imports always link.
// Area constants carry the real values. UI components are plain function
// components that render their children (EmptyState mirrors the real one and
// renders only title + description). `host` is a recording fake (../host-fake.mjs);
// useQuery/useMutation/queryClient come from the shallow renderer.
import { jsx } from './jsx-runtime.mjs'
import { atom, computed, host } from '../host-fake.mjs'
import { createContext, queryClient, useMutation, useQuery, useQueryClient } from '../render.mjs'

/** Calls recorded by stubbed SDK functions (e.g. writeClipboardText). */
export const sdkCalls = []

function uiStub(name, render) {
  const component = { [name]: props => render(props ?? {}) }[name]

  component.displayName = name

  return component
}

const J_ELAPSED = deltaMs => {
  const a = Math.abs(deltaMs)

  return a >= 86_400_000
    ? { unit: 'day', value: Math.floor(a / 86_400_000) }
    : a >= 3_600_000
      ? { unit: 'hour', value: Math.floor(a / 3_600_000) }
      : a >= 60_000
        ? { unit: 'minute', value: Math.floor(a / 60_000) }
        : { unit: 'second', value: Math.floor(a / 1000) }
}

export const REASONING_EFFORTS = Object.freeze(['minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'])
export const $accentOverride = atom(null)
export const APPEARANCE_AREAS = Object.freeze({ extra: 'appearance.extra' })
export const APPROVAL_RESPOND_TIMEOUT_MS = 300_000
export const BOT_CHAT_SESSION_HYDRATION_TIMEOUT_MS = 60_000
export const Badge = uiStub('Badge', ({ children }) => jsx('span', { children }))
export const Blobatar = uiStub('Blobatar', () => null)
export const Button = uiStub('Button', ({ children }) => children ?? null)
export const CHAT_EMPTY_AREA = 'chat.empty'
export const COMPOSER_AREAS = Object.freeze({ top: 'composer.top', bottom: 'composer.bottom', underside: 'composer.underside', leading: 'composer.leading', actions: 'composer.actions', middleware: 'composer.middleware', attachments: 'composer.attachments', microActions: 'composer.microActions', atCompletions: 'composer.atCompletions', modelPill: 'composer.modelPill', contextMenu: 'composer.contextMenu' })
export const CapabilitiesView = uiStub('CapabilitiesView', ({ children }) => children ?? null)
export const Checkbox = uiStub('Checkbox', props => jsx('input', { type: 'checkbox', ...props }))
export const Codicon = uiStub('Codicon', ({ name }) => jsx('span', { 'data-codicon': name }))
export const ColorSwatches = uiStub('ColorSwatches', ({ children }) => children ?? null)
export const ConfirmDialog = uiStub('ConfirmDialog', ({ open, onClose, onConfirm, title, description, children, confirmLabel, cancelLabel }) => open === false ? null : jsx('div', { role: 'alertdialog', children: [jsx('div', { children: title }, 'title'), description ? jsx('div', { children: description }, 'description') : null, children ?? null, jsx(Button, { onClick: () => onClose?.(), children: cancelLabel ?? 'Cancel' }, 'cancel'), jsx(Button, { onClick: () => onConfirm?.(), children: confirmLabel ?? 'Confirm' }, 'confirm')] }))
export const ConnectionGlyph = uiStub('ConnectionGlyph', ({ children }) => children ?? null)
export const ConnectorsTab = uiStub('ConnectorsTab', ({ children }) => children ?? null)
export const ContextMenu = uiStub('ContextMenu', ({ children }) => children ?? null)
export const ContextMenuCheckboxItem = uiStub('ContextMenuCheckboxItem', ({ children }) => children ?? null)
export const ContextMenuContent = uiStub('ContextMenuContent', ({ children }) => children ?? null)
export const ContextMenuItem = uiStub('ContextMenuItem', ({ children }) => children ?? null)
export const ContextMenuSeparator = uiStub('ContextMenuSeparator', ({ children }) => children ?? null)
export const ContextMenuSub = uiStub('ContextMenuSub', ({ children }) => children ?? null)
export const ContextMenuSubContent = uiStub('ContextMenuSubContent', ({ children }) => children ?? null)
export const ContextMenuSubTrigger = uiStub('ContextMenuSubTrigger', ({ children }) => children ?? null)
export const ContextMenuTrigger = uiStub('ContextMenuTrigger', ({ children }) => children ?? null)
export const Contribute = uiStub('Contribute', () => null)
export const CopyButton = uiStub('CopyButton', ({ text }) => jsx('span', { 'data-copy': text }))
export const DEFAULT_REASONING_EFFORT = 'medium'
export const DEFAULT_SESSION_HYDRATION_TIMEOUT_MS = 20_000
export const DecodeText = uiStub('DecodeText', ({ text, children }) => text ?? children ?? null)
export const Dialog = uiStub('Dialog', ({ open, children }) => (open === false ? null : children ?? null))
export const DialogContent = uiStub('DialogContent', ({ children }) => children ?? null)
export const DialogDescription = uiStub('DialogDescription', ({ children }) => children ?? null)
export const DialogFooter = uiStub('DialogFooter', ({ children }) => children ?? null)
export const DialogHeader = uiStub('DialogHeader', ({ children }) => children ?? null)
export const DialogTitle = uiStub('DialogTitle', ({ children }) => children ?? null)
export const DialogTrigger = uiStub('DialogTrigger', ({ children }) => children ?? null)
export const DisclosureCaret = uiStub('DisclosureCaret', ({ children }) => children ?? null)
export const DropdownMenu = uiStub('DropdownMenu', ({ children }) => children ?? null)
export const DropdownMenuContent = uiStub('DropdownMenuContent', ({ children }) => children ?? null)
export const DropdownMenuItem = uiStub('DropdownMenuItem', ({ children }) => children ?? null)
export const DropdownMenuSeparator = uiStub('DropdownMenuSeparator', ({ children }) => children ?? null)
export const DropdownMenuTrigger = uiStub('DropdownMenuTrigger', ({ children }) => children ?? null)
export const EmptyState = uiStub('EmptyState', ({ title, description }) => jsx('div', { children: [jsx('div', { children: title }, 'title'), description ? jsx('div', { children: description }, 'description') : null] }))
export const ErrorState = uiStub('ErrorState', ({ title, description, children }) => jsx('div', { role: 'alert', children: [jsx('div', { children: title }, 'title'), description ? jsx('div', { children: description }, 'description') : null, children ? jsx('div', { children }, 'actions') : null] }))
export const FadeScroll = uiStub('FadeScroll', ({ children }) => children ?? null)
export const GHOST_ICON_BTN = 'ghost-icon-btn'
export const GlyphSpinner = uiStub('GlyphSpinner', () => jsx('span', { 'data-spinner': true }))
export const HYDRATION_SYNC_BADGE_TIMEOUT_MS = 30_000
export const Input = uiStub('Input', props => jsx('input', props))
export const KANBAN_CARD_AREAS = Object.freeze({ badges: 'kanban.card.badges' })
export const KANBAN_CARD_BADGE_HUES = Object.freeze(['red', 'orange', 'yellow', 'lime', 'green', 'mint', 'cyan', 'sky', 'blue', 'violet', 'magenta', 'pink'])
export const KANBAN_CARD_BADGE_LIMITS = Object.freeze({ labelChars: 24, perPlugin: 2, tooltipChars: 160 })
export const KEYBINDS_AREA = 'keybinds'
export const KanbanCardBadges = uiStub('KanbanCardBadges', ({ children }) => children ?? null)
export const Kbd = uiStub('Kbd', ({ children }) => jsx('kbd', { children }))
export const KbdGroup = uiStub('KbdGroup', ({ children }) => children ?? null)
export const ListRow = uiStub('ListRow', ({ children }) => children ?? null)
export const Loader = uiStub('Loader', () => jsx('span', { 'data-spinner': true }))
export const LocalizedTabTitle = uiStub('LocalizedTabTitle', ({ children }) => children ?? null)
export const LogView = uiStub('LogView', ({ children }) => jsx('pre', { children }))
export class LruCache { constructor(max = 100) { this.max = max; this.map = new Map() } get(k) { if (!this.map.has(k)) return undefined; const v = this.map.get(k); this.map.delete(k); this.map.set(k, v); return v } set(k, v) { this.map.delete(k); this.map.set(k, v); if (this.map.size > this.max) this.map.delete(this.map.keys().next().value); return this } has(k) { return this.map.has(k) } delete(k) { return this.map.delete(k) } clear() { this.map.clear() } get size() { return this.map.size } }
export const MODEL_MENU_ROW_AREA = 'modelMenu.row'
export const MessageTextContent = uiStub('MessageTextContent', ({ children }) => children ?? null)
export const ModelCatalogMenu = uiStub('ModelCatalogMenu', ({ children }) => children ?? null)
export const ModelMenuCloseContext = createContext(() => {})
export const PALETTE_AREA = 'palette'
export const PANES_AREA = 'panes'
export const PLUGIN_APP_ACTIONS = Object.freeze(['view.showBrowser', 'view.showFiles', 'view.toggleSidebar', 'view.toggleRightSidebar', 'view.toggleReview', 'view.toggleStatusbar', 'view.findInPage', 'composer.focus', 'session.new', 'session.focusSearch', 'conversation.scrollPageUp', 'conversation.scrollPageDown', 'nav.commandPalette', 'nav.settings', 'nav.profiles', 'nav.capabilities', 'nav.messaging', 'nav.artifacts', 'nav.cron', 'nav.agents', 'keybinds.openPanel'])
export const PRIMARY_ICON_BTN = 'primary-icon-btn'
export const PROFILE_SWATCHES = Object.freeze(Array.from({ length: 12 }, (_, index) => `hsl(${index * 30} 60% 55%)`))
export const PanelAction = uiStub('PanelAction', ({ children }) => children ?? null)
export const PanelAddButton = uiStub('PanelAddButton', ({ children }) => children ?? null)
export const PanelBlock = uiStub('PanelBlock', ({ children }) => children ?? null)
export const PanelBody = uiStub('PanelBody', ({ children }) => children ?? null)
export const PanelDetail = uiStub('PanelDetail', ({ children }) => children ?? null)
export const PanelEmpty = uiStub('PanelEmpty', ({ children }) => children ?? null)
export const PanelHeader = uiStub('PanelHeader', ({ children }) => children ?? null)
export const PanelList = uiStub('PanelList', ({ children }) => children ?? null)
export const PanelListRow = uiStub('PanelListRow', ({ children }) => children ?? null)
export const PanelMeta = uiStub('PanelMeta', ({ children }) => children ?? null)
export const PanelPill = uiStub('PanelPill', ({ children }) => children ?? null)
export const PanelRowMenu = uiStub('PanelRowMenu', ({ children }) => children ?? null)
export const PanelSectionLabel = uiStub('PanelSectionLabel', ({ children }) => children ?? null)
export const Popover = uiStub('Popover', ({ children }) => children ?? null)
export const PopoverContent = uiStub('PopoverContent', ({ children }) => children ?? null)
export const PopoverTrigger = uiStub('PopoverTrigger', ({ children }) => children ?? null)
export const REASONING_EFFORT_VALUES = Object.freeze(['none', ...REASONING_EFFORTS])
export const ROUTES_AREA = 'routes'
export const RowButton = uiStub('RowButton', ({ children }) => children ?? null)
export const SESSION_ROW_AREAS = Object.freeze({ leading: 'sessionRow.leading', trailing: 'sessionRow.trailing' })
export const SETTINGS_PLUGINS_AREA = 'settings.plugins'
export const SIDEBAR_NAV_AREA = 'sidebar.nav'
export const SIDEBAR_NAV_PREFS_AREA = 'sidebarNav.prefs'
export const SIDEBAR_PROFILE_GROUP_HEADER_AREA = 'sidebar.profileGroup.header'
export const SIDEBAR_ROW_LEAD = 'grid size-3.5 shrink-0 place-items-center'
export const SIDEBAR_TRUNCATED_LEADING = 'leading-[1.35]'
export const STATUSBAR_AREAS = Object.freeze({ left: 'statusBar.left', right: 'statusBar.right' })
export const SandboxedFrame = uiStub('SandboxedFrame', ({ src, title }) => jsx('iframe', { src, title }))
export const ScrollArea = uiStub('ScrollArea', ({ children }) => children ?? null)
export const SearchField = uiStub('SearchField', props => jsx('input', props))
export const SegmentedControl = uiStub('SegmentedControl', ({ options = [], value, onChange }) => jsx('div', { role: 'radiogroup', children: options.map(option => jsx(Button, { onClick: () => onChange?.(option.id), 'aria-pressed': option.id === value, children: option.label }, option.id)) }))
export const Select = uiStub('Select', ({ children }) => children ?? null)
export const SelectContent = uiStub('SelectContent', ({ children }) => children ?? null)
export const SelectItem = uiStub('SelectItem', ({ children }) => children ?? null)
export const SelectTrigger = uiStub('SelectTrigger', ({ children }) => children ?? null)
export const SelectValue = uiStub('SelectValue', () => null)
export const Separator = uiStub('Separator', () => jsx('hr', {}))
export const SessionStatusDot = uiStub('SessionStatusDot', ({ children }) => children ?? null)
export const SidebarRowLead = uiStub('SidebarRowLead', ({ children }) => children ?? null)
export const Skeleton = uiStub('Skeleton', () => jsx('span', { 'data-skeleton': true }))
export const StatusDot = uiStub('StatusDot', props => jsx('span', { 'data-tone': props.tone }))
export const Streamdown = uiStub('Streamdown', ({ children }) => jsx('div', { children }))
export const Switch = uiStub('Switch', props => jsx('input', { type: 'checkbox', ...props }))
export const THEMES_AREA = 'themes'
export const TITLEBAR_AREAS = Object.freeze({ center: 'titleBar.center', left: 'titleBar.left', right: 'titleBar.right' })
export const TOOL_MEDIA_RENDERER_AREA = 'transcript.toolMedia'
export const TRANSCRIPT_DIRECTIVE_AREA = 'transcript.directives'
export const Tabs = uiStub('Tabs', ({ children }) => children ?? null)
export const TabsList = uiStub('TabsList', ({ children }) => children ?? null)
export const TabsTrigger = uiStub('TabsTrigger', ({ children }) => children ?? null)
export const Textarea = uiStub('Textarea', props => jsx('textarea', props))
export const Tip = uiStub('Tip', ({ children }) => children ?? null)
export const ToggleRow = uiStub('ToggleRow', ({ children }) => children ?? null)
export const ToolsetConfigPanel = uiStub('ToolsetConfigPanel', ({ children }) => children ?? null)
export const Tooltip = uiStub('Tooltip', ({ children }) => children ?? null)
export const TooltipContent = uiStub('TooltipContent', () => null)
export const TooltipProvider = uiStub('TooltipProvider', ({ children }) => children ?? null)
export const TooltipTrigger = uiStub('TooltipTrigger', ({ children }) => children ?? null)
export const WORKSPACE_PAGE_HEADER_AREA = 'workspace.pageHeader'
export const Wordmark = uiStub('Wordmark', () => null)
export const WorkspacePageHeaderControl = uiStub('WorkspacePageHeaderControl', ({ children }) => children ?? null)
export const ackStoredSessionId = () => {}
export const blobatarSvg = () => ''
export const captureGatewayFileDownload = async () => null
export const catalogProviderMatches = () => false
export const cn = (...args) => args.flat(Infinity).flatMap(a => (!a ? [] : typeof a === 'string' ? [a] : typeof a === 'object' ? Object.entries(a).filter(([, on]) => on).map(([k]) => k) : [])).join(' ')
export const coarseElapsed = deltaMs => { const a = Math.abs(deltaMs); return a >= 86_400_000 ? { unit: 'day', value: Math.floor(a / 86_400_000) } : a >= 3_600_000 ? { unit: 'hour', value: Math.floor(a / 3_600_000) } : a >= 60_000 ? { unit: 'minute', value: Math.floor(a / 60_000) } : { unit: 'second', value: Math.floor(a / 1000) } }
export const compactNumber = n => new Intl.NumberFormat('en-US', { notation: 'compact' }).format(n)
export const composerInputBacking = 'composer-input-backing'
export const composerSurfaceRing = 'composer-surface-ring'
export const contrastRatio = () => 21
export const createBudgetedLoop = () => ({ start() {}, stop() {}, run() {} })
export const evaluateRuntimeReadiness = () => ({ ready: true })
export const fmtDateTime = new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC' })
export const fmtDayTime = new Intl.DateTimeFormat('en-US', { day: 'numeric', hour: 'numeric', minute: '2-digit', month: 'short', timeZone: 'UTC' })
export const forgetSessionUnread = () => {}
export const formatAgo = (fromMs, _labels, nowMs = Date.now()) => { const { unit, value } = ((d) => J_ELAPSED(d))(nowMs - fromMs); return `${value}${unit[0]}` }
export const formatModifierToken = token => token
export const gatewayActivationEpoch = atom(0)
export const haptic = () => {}
export const hexToOklch = () => ({ l: 0, c: 0, h: 0 })
export const hueDelta = () => 0
export const icons = new Proxy({}, { get: (target, key) => (typeof key === 'string' ? (target[key] ??= uiStub(key, () => null)) : undefined) })
export const isSubmitEnter = event => event?.key === 'Enter' && !event.shiftKey
export const markSessionUnreadFinished = () => {}
export const maxChroma = () => 0
export const mixOklab = color => color
export const nextRunOverdueMs = () => null
export const normalizeHex = value => value
export const oklchToHex = () => ''
export const oklchToSrgb255 = () => [0, 0, 0]
export const pluginSettingsHref = (id) => `#/settings?plugin=${encodeURIComponent(id)}`
export const profileColor = name => (!name || name === 'default' ? null : 'hsl(200 60% 55%)')
export const profileColorSoft = color => color
export const randomUuid = () => globalThis.crypto.randomUUID()
export const readableOn = () => ''
export const reasoningEffortLabel = value => String(value)
export const relativeTime = (targetMs, nowMs = Date.now()) => { const s = Math.round((targetMs - nowMs) / 1000); const a = Math.abs(s); const [v, u] = a < 60 ? [a, 'sec'] : a < 3600 ? [Math.round(a / 60), 'min'] : a < 86400 ? [Math.round(a / 3600), 'hr'] : [Math.round(a / 86400), 'day']; return s <= 0 ? `${v} ${u} ago` : `in ${v} ${u}` }
export const requestTheme = () => {}
export const resolveSiblingWsUrl = () => null
export const retintTheme = theme => theme
export const setAccentOverride = () => {}
export const surfaceModelSwitchConfirm = async () => true
export const themeHue = () => 0
export const translateNow = key => String(key)
export const useGatewayActivationEpoch = () => 0
export const useGrabScroll = () => ({})
export const useI18n = () => ({ t: new Proxy({}, { get: () => new Proxy({}, { get: (_, key) => String(key) }) }), locale: 'en' })
export const usePluginI18n = () => ({ t: key => String(key), locale: 'en' })
export const useTheme = () => ({ theme: { id: 'test' }, themes: [] })
export const useValue = store => store.get()
export const writeClipboardText = async text => { sdkCalls.push({ method: 'writeClipboardText', args: [text] }); return true }

export { atom, computed, host, queryClient, useMutation, useQuery, useQueryClient }
