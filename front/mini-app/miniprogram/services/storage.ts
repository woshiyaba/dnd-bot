import type { RecentSession } from '../types/session'

const RECENT_SESSION_KEY = 'dnd_recent_session'
const DRAFT_PREFIX = 'dnd_draft_'

export function loadRecentSession(): RecentSession | null {
  return (wx.getStorageSync(RECENT_SESSION_KEY) as RecentSession | undefined) ?? null
}

export function saveRecentSession(session: RecentSession): void {
  wx.setStorageSync(RECENT_SESSION_KEY, session)
}

export function clearRecentSession(): void {
  wx.removeStorageSync(RECENT_SESSION_KEY)
}

export function loadDraft(roomId: string): string {
  return (wx.getStorageSync(`${DRAFT_PREFIX}${roomId}`) as string | undefined) ?? ''
}

export function saveDraft(roomId: string, draft: string): void {
  wx.setStorageSync(`${DRAFT_PREFIX}${roomId}`, draft)
}
