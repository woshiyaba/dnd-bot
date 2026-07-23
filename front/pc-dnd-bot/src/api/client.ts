import type {
  CharacterOption,
  DiceRollResult,
  DiceType,
  RoomAuthResponse,
  RoomCredential,
  RoomLobbyView,
  SessionView,
} from '../types/game'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:32388'

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function requestJson<T>(
  path: string,
  init: RequestInit = {},
  accessToken?: string,
): Promise<T> {
  const headers = new Headers(init.headers)
  if (init.body) {
    headers.set('Content-Type', 'application/json')
  }
  if (accessToken) {
    headers.set('Authorization', `Bearer ${accessToken}`)
  }
  const response = await fetch(`${API_BASE_URL}${path}`, { ...init, headers })
  if (!response.ok) {
    let message = `请求失败（${response.status}）`
    try {
      const data = (await response.json()) as { detail?: string }
      if (data.detail) message = data.detail
    } catch {
      // 保留统一错误文本。
    }
    throw new ApiError(response.status, message)
  }
  return response.json() as Promise<T>
}

function post<T>(path: string, body: unknown, accessToken?: string) {
  return requestJson<T>(
    path,
    { method: 'POST', body: JSON.stringify(body) },
    accessToken,
  )
}

export const gameApi = {
  characters: () => requestJson<CharacterOption[]>('/api/rooms/characters'),
  lobby: (roomCode: string) =>
    requestJson<RoomLobbyView>(`/api/rooms/${roomCode}/lobby`),
  createRoom: (displayName: string, characterId: string) =>
    post<RoomAuthResponse>('/api/rooms', {
      display_name: displayName,
      character_id: characterId,
      campaign_id: 'whispers_bell_tower',
    }),
  joinRoom: (roomCode: string, displayName: string, characterId: string) =>
    post<RoomAuthResponse>(`/api/rooms/${roomCode}/join`, {
      display_name: displayName,
      character_id: characterId,
    }),
  session: (credential: RoomCredential) =>
    requestJson<SessionView>(
      `/api/rooms/${credential.roomCode}`,
      {},
      credential.accessToken,
    ),
  start: (credential: RoomCredential) =>
    post<SessionView>(
      `/api/rooms/${credential.roomCode}/start`,
      { opening: '我们推开破钟酒馆的门，走向等候已久的村长。' },
      credential.accessToken,
    ),
  message: (credential: RoomCredential, content: string) =>
    post<SessionView>(
      `/api/rooms/${credential.roomCode}/messages`,
      { content },
      credential.accessToken,
    ),
  action: (credential: RoomCredential, action: Record<string, unknown>) =>
    post<SessionView>(
      `/api/rooms/${credential.roomCode}/actions`,
      action,
      credential.accessToken,
    ),
  interactionRoll: (credential: RoomCredential) =>
    post<{ roll: DiceRollResult; session: SessionView }>(
      `/api/rooms/${credential.roomCode}/interactions/roll`,
      {},
      credential.accessToken,
    ),
  freeRoll: (credential: RoomCredential, diceType: DiceType) =>
    post<DiceRollResult>(
      `/api/rooms/${credential.roomCode}/dice/roll`,
      { dice_type: diceType },
      credential.accessToken,
    ),
}

export function roomWebSocketUrl(credential: RoomCredential) {
  const url = new URL(API_BASE_URL, window.location.origin)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  url.pathname = `/ws/rooms/${credential.roomCode}`
  url.search = new URLSearchParams({ token: credential.accessToken }).toString()
  return url.toString()
}
