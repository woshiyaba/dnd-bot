import { API_BASE_URL, DEFAULT_CAMPAIGN_ID, DEFAULT_USER_ID } from '../config/env'
import type { SessionPayload } from '../types/session'

interface ApiErrorBody {
  detail?: string
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly statusCode: number,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

function request<T>(path: string, method: 'GET' | 'POST', data?: unknown): Promise<T> {
  return new Promise((resolve, reject) => {
    wx.request({
      url: `${API_BASE_URL.replace(/\/$/, '')}${path}`,
      method,
      data: data as WechatMiniprogram.IAnyObject | undefined,
      timeout: 120000,
      header: { 'content-type': 'application/json' },
      success(response) {
        if (response.statusCode >= 200 && response.statusCode < 300) {
          resolve(response.data as T)
          return
        }
        const body = response.data as ApiErrorBody
        reject(
          new ApiError(
            body?.detail || `请求失败（HTTP ${response.statusCode}）`,
            response.statusCode,
          ),
        )
      },
      fail(error) {
        reject(new Error(error.errMsg || '无法连接后端'))
      },
    })
  })
}

export function createSession(roomId: string): Promise<SessionPayload> {
  return request('/session/start', 'POST', {
    room_id: roomId,
    user_id: DEFAULT_USER_ID,
    campaign_id: DEFAULT_CAMPAIGN_ID,
    dm_mode: 'llm',
    opening: '我推开破钟酒馆的门，走向村长。',
    random_seed: 20260626,
  })
}

export function restoreSession(roomId: string): Promise<SessionPayload> {
  return request(`/session/${encodeURIComponent(roomId)}/state`, 'GET')
}

export function sendSessionMessage(
  roomId: string,
  userInput: string,
): Promise<SessionPayload> {
  return request(`/session/${encodeURIComponent(roomId)}/message`, 'POST', {
    user_id: DEFAULT_USER_ID,
    user_input: userInput,
  })
}

export function submitSessionInteraction(
  roomId: string,
  resumeValue: Record<string, unknown>,
): Promise<SessionPayload> {
  return request(`/session/${encodeURIComponent(roomId)}/submit`, 'POST', {
    user_id: DEFAULT_USER_ID,
    resume_value: resumeValue,
  })
}

export function rollSessionDice(roomId: string): Promise<SessionPayload> {
  return request(`/session/${encodeURIComponent(roomId)}/roll`, 'POST', {
    user_id: DEFAULT_USER_ID,
  })
}
