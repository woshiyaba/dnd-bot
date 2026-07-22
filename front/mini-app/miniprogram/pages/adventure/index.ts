import { loadDraft, loadRecentSession, saveDraft } from '../../services/storage'
import { sessionStore, timelineFromState } from '../../store/session'
import type { SessionStoreState } from '../../store/session'
import type {
  CheckResult,
  Combatant,
  CombatView,
  InterruptRequest,
  LastCombat,
  RollResult,
  SceneState,
  TimelineMessage,
  UiStatus,
} from '../../types/session'

let unsubscribe: (() => void) | null = null

Page({
  data: {
    roomId: '',
    scene: {} as SceneState,
    messages: [] as TimelineMessage[],
    streamText: '',
    party: [] as Combatant[],
    leadCharacter: null as Combatant | null,
    interrupt: null as InterruptRequest | null,
    combat: null as CombatView | null,
    lastCheck: null as CheckResult | null,
    lastCombat: null as LastCombat | null,
    rollResult: null as RollResult | null,
    uiStatus: 'restoring' as UiStatus,
    connection: 'offline',
    error: '',
    draft: '',
    composerDisabled: true,
    composerPlaceholder: '正在恢复冒险…',
    finished: false,
    scrollTop: 0,
  },
  onLoad(options: Record<string, string | undefined>) {
    const recent = loadRecentSession()
    const roomId = options.roomId ? decodeURIComponent(options.roomId) : recent?.roomId || ''
    if (!roomId) {
      wx.redirectTo({ url: '/pages/home/index' })
      return
    }
    this.setData({ roomId, draft: loadDraft(roomId) })
    unsubscribe = sessionStore.subscribe((state) => this.renderStore(state))
    sessionStore.connect()
    const current = sessionStore.snapshot()
    if (current.roomId !== roomId || !current.payload) {
      sessionStore.restore(roomId).catch(() => undefined)
    }
  },
  onUnload() {
    unsubscribe?.()
    unsubscribe = null
  },
  onShow() {
    if (this.data.roomId && !unsubscribe) {
      unsubscribe = sessionStore.subscribe((state) => this.renderStore(state))
    }
  },
  renderStore(state: SessionStoreState) {
    if (state.roomId && this.data.roomId && state.roomId !== this.data.roomId) return
    const payload = state.payload
    const sessionState = payload?.state
    const party = Object.values(sessionState?.party ?? {})
    const interrupt = payload?.interrupt ?? null
    const combat = interrupt?.extra?.combat ?? null
    const isAwaitingInput = state.uiStatus === 'awaiting_input'
    this.setData({
      scene: sessionState?.scene ?? {},
      messages: timelineFromState(state),
      streamText: state.streamText,
      party,
      leadCharacter: party[0] ?? null,
      interrupt,
      combat,
      lastCheck: payload?.last_check ?? sessionState?.last_check ?? null,
      lastCombat: payload?.last_combat ?? sessionState?.last_combat ?? null,
      rollResult: state.rollResult,
      uiStatus: state.uiStatus,
      connection: state.connection,
      error: state.error,
      composerDisabled: !isAwaitingInput,
      composerPlaceholder: composerPlaceholder(state.uiStatus),
      finished: state.uiStatus === 'finished',
      scrollTop: Date.now(),
    })
  },
  draftChange(event: WechatMiniprogram.CustomEvent<{ value: string }>) {
    const draft = event.detail.value
    this.setData({ draft })
    saveDraft(this.data.roomId, draft)
  },
  send() {
    const message = this.data.draft.trim()
    if (!message) return
    this.setData({ draft: '' })
    saveDraft(this.data.roomId, '')
    sessionStore.sendMessage(message).catch(() => {
      this.setData({ draft: message })
      saveDraft(this.data.roomId, message)
    })
  },
  roll() {
    sessionStore.roll().catch(() => undefined)
  },
  submitManual(event: WechatMiniprogram.CustomEvent<{ value: number; damage: boolean }>) {
    const { value, damage } = event.detail
    if (!Number.isInteger(value) || (damage ? value < 0 : value < 1 || value > 20)) {
      wx.showToast({
        title: damage ? '伤害必须是非负整数' : '请输入 1–20 的整数',
        icon: 'none',
      })
      return
    }
    sessionStore.submit(damage ? { result: value } : { d20: value }).catch(() => undefined)
  },
  submitAction(event: WechatMiniprogram.CustomEvent<Record<string, unknown>>) {
    sessionStore.submit(event.detail).catch(() => undefined)
  },
  retry() {
    sessionStore.restore(this.data.roomId).catch(() => undefined)
  },
  openCharacter() {
    wx.navigateTo({ url: '/pages/character/index' })
  },
  openEnding() {
    wx.navigateTo({ url: '/pages/ending/index' })
  },
})

function composerPlaceholder(status: string): string {
  if (status === 'finished') return '冒险已经结束'
  if (status === 'awaiting_interaction') return '请先完成当前操作'
  if (status === 'error') return '恢复连接后再继续'
  if (status !== 'awaiting_input') return 'DM 正在回应…'
  return '描述你的行动…'
}
