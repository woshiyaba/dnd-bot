import { DEFAULT_USER_ID } from '../config/env'
import {
  ApiError,
  createSession,
  restoreSession,
  rollSessionDice,
  sendSessionMessage,
  submitSessionInteraction,
} from '../services/api'
import { sessionSocket } from '../services/socket'
import { clearRecentSession, saveRecentSession } from '../services/storage'
import type {
  Combatant,
  ConnectionStatus,
  RollResult,
  SessionPayload,
  SocketMessage,
  TimelineMessage,
  UiStatus,
} from '../types/session'

export interface SessionStoreState {
  roomId: string
  payload: SessionPayload | null
  uiStatus: UiStatus
  connection: ConnectionStatus
  streamText: string
  error: string
  rollResult: RollResult | null
}

type Listener = (state: SessionStoreState) => void

class SessionStore {
  private listeners = new Set<Listener>()
  private state: SessionStoreState = {
    roomId: '',
    payload: null,
    uiStatus: 'restoring',
    connection: 'offline',
    streamText: '',
    error: '',
    rollResult: null,
  }
  private reconnectRestore = false

  initialize(): void {
    // 全局 store 延迟到冒险页再连接 WebSocket，避免首页常驻连接。
  }

  snapshot(): SessionStoreState {
    return this.state
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener)
    listener(this.state)
    return () => this.listeners.delete(listener)
  }

  connect(): void {
    if (this.state.connection !== 'offline') return
    this.patch({ connection: 'connecting' })
    sessionSocket.connect(DEFAULT_USER_ID, {
      onOpen: () => {
        const shouldRestore = this.reconnectRestore && Boolean(this.state.roomId)
        this.reconnectRestore = false
        this.patch({ connection: 'online' })
        if (shouldRestore) void this.restore(this.state.roomId, true)
      },
      onClose: () => {
        this.reconnectRestore = true
        this.patch({
          connection: 'connecting',
          uiStatus:
            this.state.payload?.status === 'finished' ? 'finished' : 'reconnecting',
        })
      },
      onMessage: (message) => this.handleSocketMessage(message),
    })
  }

  async start(roomId: string): Promise<void> {
    this.patch({
      roomId,
      payload: null,
      uiStatus: 'sending',
      streamText: '',
      error: '',
      rollResult: null,
    })
    this.connect()
    await this.execute(() => createSession(roomId))
  }

  async restore(roomId: string, quiet = false): Promise<void> {
    this.patch({
      roomId,
      uiStatus: quiet ? this.state.uiStatus : 'restoring',
      error: '',
    })
    this.connect()
    try {
      const payload = await restoreSession(roomId)
      this.applyPayload(payload)
    } catch (error) {
      if (error instanceof ApiError && error.statusCode === 404) {
        clearRecentSession()
      }
      this.patch({
        uiStatus: 'error',
        error: error instanceof Error ? error.message : '恢复会话失败',
        streamText: '',
      })
      if (!quiet) throw error
    }
  }

  async sendMessage(userInput: string): Promise<void> {
    if (!this.state.roomId || this.state.uiStatus !== 'awaiting_input') return
    this.patch({ uiStatus: 'sending', streamText: '', error: '', rollResult: null })
    await this.execute(() => sendSessionMessage(this.state.roomId, userInput))
  }

  async submit(resumeValue: Record<string, unknown>): Promise<void> {
    if (!this.state.roomId || this.state.uiStatus !== 'awaiting_interaction') return
    this.patch({ uiStatus: 'resolving', streamText: '', error: '', rollResult: null })
    await this.execute(() =>
      submitSessionInteraction(this.state.roomId, resumeValue),
    )
  }

  async roll(): Promise<void> {
    if (!this.state.roomId || this.state.uiStatus !== 'awaiting_interaction') return
    this.patch({ uiStatus: 'resolving', streamText: '', error: '', rollResult: null })
    await this.execute(() => rollSessionDice(this.state.roomId))
  }

  private async execute(request: () => Promise<SessionPayload>): Promise<void> {
    try {
      const payload = await request()
      this.applyPayload(payload)
    } catch (error) {
      this.patch({
        uiStatus: 'error',
        error: error instanceof Error ? error.message : '请求失败，请稍后重试',
        streamText: '',
      })
      throw error
    }
  }

  private handleSocketMessage(message: SocketMessage): void {
    const messageRoomId =
      message.room_id || (message.payload as SessionPayload | undefined)?.room_id
    if (messageRoomId && messageRoomId !== this.state.roomId) return

    if (message.type === 'node_start' && this.isPublicNarrationNode(message.node)) {
      this.patch({ uiStatus: 'streaming', streamText: '' })
      return
    }
    if (message.type === 'stream' && this.isPublicNarrationNode(message.node)) {
      this.patch({
        uiStatus: 'streaming',
        streamText: `${this.state.streamText}${message.content ?? ''}`,
      })
      return
    }
    if (message.type === 'roll_result' && message.payload) {
      this.patch({ rollResult: message.payload as RollResult, uiStatus: 'resolving' })
      return
    }
    if (
      (message.type === 'session_start' || message.type === 'session_update') &&
      message.payload
    ) {
      this.applyPayload(message.payload as SessionPayload)
    }
  }

  private isPublicNarrationNode(node?: string): boolean {
    return node === 'dm' || node === 'narrate'
  }

  private applyPayload(payload: SessionPayload): void {
    const uiStatus = this.payloadStatus(payload)
    const rollResult = payload.roll_result || this.state.rollResult
    const pendingType = payload.interrupt?.interrupt_type
    this.patch({
      roomId: payload.room_id || this.state.roomId,
      payload: normalizePayload(payload),
      uiStatus,
      streamText: '',
      error: '',
      rollResult:
        rollResult?.interrupt_type && rollResult.interrupt_type === pendingType
          ? rollResult
          : null,
    })
    const scene = payload.state?.scene
    if (payload.room_id && payload.status) {
      saveRecentSession({
        roomId: payload.room_id,
        title: '钟楼下的低语',
        location: scene?.location || '未知地点',
        status: payload.status,
        updatedAt: Date.now(),
      })
    }
  }

  private payloadStatus(payload: SessionPayload): UiStatus {
    if (payload.status === 'interrupted') return 'awaiting_interaction'
    if (payload.status === 'finished') return 'finished'
    return 'awaiting_input'
  }

  private patch(patch: Partial<SessionStoreState>): void {
    this.state = { ...this.state, ...patch }
    this.listeners.forEach((listener) => listener(this.state))
  }
}

function normalizePayload(payload: SessionPayload): SessionPayload {
  const combat = payload.interrupt?.extra?.combat
  if (combat?.combatants) {
    combat.combatants = combat.combatants.map(normalizeCombatant)
    combat.current_actor_name = combat.combatants.find(
      (item) => item.id === combat.current_actor_id,
    )?.name
  }
  if (payload.state?.party) {
    payload.state.party = Object.fromEntries(
      Object.entries(payload.state.party).map(([id, combatant]) => [
        id,
        normalizeCombatant(combatant),
      ]),
    )
  }
  return payload
}

function normalizeCombatant(combatant: Combatant): Combatant {
  const maxHp = Math.max(combatant.max_hp ?? 1, 1)
  const hp = Math.max(0, Math.min(combatant.current_hp ?? maxHp, maxHp))
  return {
    ...combatant,
    current_hp: hp,
    max_hp: maxHp,
    hp_percent: Math.round((hp / maxHp) * 100),
  }
}

export function timelineFromState(state: SessionStoreState): TimelineMessage[] {
  return state.payload?.state?.messages ?? []
}

export const sessionStore = new SessionStore()
