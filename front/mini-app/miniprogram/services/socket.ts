import { websocketBaseUrl } from '../config/env'
import type { SocketMessage } from '../types/session'

export interface SocketHandlers {
  onOpen: () => void
  onClose: () => void
  onMessage: (message: SocketMessage) => void
}

class SessionSocket {
  private task: WechatMiniprogram.SocketTask | null = null
  private handlers: SocketHandlers | null = null
  private userId = ''
  private reconnectTimer: number | null = null
  private heartbeatTimer: number | null = null
  private reconnectAttempt = 0
  private intentionallyClosed = false

  connect(userId: string, handlers: SocketHandlers): void {
    this.handlers = handlers
    this.userId = userId
    this.intentionallyClosed = false
    if (this.task) return
    this.open()
  }

  close(): void {
    this.intentionallyClosed = true
    this.clearTimers()
    this.task?.close({ code: 1000, reason: 'page hidden' })
    this.task = null
  }

  private open(): void {
    const url = `${websocketBaseUrl().replace(/\/$/, '')}/ws/${encodeURIComponent(this.userId)}`
    const task = wx.connectSocket({ url })
    this.task = task

    task.onOpen(() => {
      this.reconnectAttempt = 0
      this.handlers?.onOpen()
      this.heartbeatTimer = setInterval(() => {
        task.send({ data: 'ping' })
      }, 25000) as unknown as number
    })
    task.onMessage((event) => {
      if (typeof event.data !== 'string') return
      try {
        this.handlers?.onMessage(JSON.parse(event.data) as SocketMessage)
      } catch {
        // 非 JSON 服务端消息不属于公开协议，直接忽略。
      }
    })
    task.onError(() => this.handleDisconnect(task))
    task.onClose(() => this.handleDisconnect(task))
  }

  private handleDisconnect(task: WechatMiniprogram.SocketTask): void {
    if (this.task !== task) return
    this.task = null
    if (this.heartbeatTimer !== null) {
      clearInterval(this.heartbeatTimer)
      this.heartbeatTimer = null
    }
    this.handlers?.onClose()
    if (this.intentionallyClosed || this.reconnectTimer !== null) return
    const delay = Math.min(1000 * 2 ** this.reconnectAttempt, 15000)
    this.reconnectAttempt += 1
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      this.open()
    }, delay) as unknown as number
  }

  private clearTimers(): void {
    if (this.reconnectTimer !== null) clearTimeout(this.reconnectTimer)
    if (this.heartbeatTimer !== null) clearInterval(this.heartbeatTimer)
    this.reconnectTimer = null
    this.heartbeatTimer = null
  }
}

export const sessionSocket = new SessionSocket()
