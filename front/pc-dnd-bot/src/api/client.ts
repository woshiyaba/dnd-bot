import type {
  CharacterCreationCatalog,
  CharacterDraft,
  DiceRollResult,
  DiceType,
  RoomAuthResponse,
  RoomCredential,
  RoomLobbyView,
  SessionView,
  StoryConversationMessage,
  StoryDraftResponse,
  StoryGenerationTaskResponse,
  StoryInterviewResponse,
  StorySummary,
} from '../types/game'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:32388'

export class ApiError extends Error {
  status: number
  code?: string
  detail?: unknown

  constructor(status: number, message: string, code?: string, detail?: unknown) {
    super(message)
    this.status = status
    this.code = code
    this.detail = detail
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
    let code: string | undefined
    let detail: unknown
    try {
      const data = (await response.json()) as {
        detail?: string | { code?: string; message?: string }
      }
      if (typeof data.detail === 'string') message = data.detail
      if (data.detail && typeof data.detail === 'object') {
        message = data.detail.message ?? message
        code = data.detail.code
        detail = data.detail
      }
    } catch {
      // 保留统一错误文本。
    }
    throw new ApiError(response.status, message, code, detail)
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
  characterOptions: () =>
    requestJson<CharacterCreationCatalog>('/api/rooms/character-options'),
  lobby: (roomCode: string) =>
    requestJson<RoomLobbyView>(`/api/rooms/${roomCode}/lobby`),
  createRoom: (
    displayName: string,
    character: CharacterDraft,
    campaignId: string,
  ) =>
    post<RoomAuthResponse>('/api/rooms', {
      display_name: displayName,
      character,
      campaign_id: campaignId,
    }),
  joinRoom: (
    roomCode: string,
    displayName: string,
    character: CharacterDraft,
  ) =>
    post<RoomAuthResponse>(`/api/rooms/${roomCode}/join`, {
      display_name: displayName,
      character,
    }),
  session: (credential: RoomCredential) =>
    requestJson<SessionView>(
      `/api/rooms/${credential.roomCode}`,
      {},
      credential.accessToken,
    ),
  start: (credential: RoomCredential, confirmPlayerCountMismatch = false) =>
    post<SessionView>(
      `/api/rooms/${credential.roomCode}/start`,
      {
        opening: '冒险者们已经集结，准备踏入这段未知的旅程。',
        confirm_player_count_mismatch: confirmPlayerCountMismatch,
      },
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
  levelUp: (
    credential: RoomCredential,
    increases: Record<string, number>,
  ) =>
    post<SessionView>(
      `/api/rooms/${credential.roomCode}/level-ups`,
      { increases },
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
  stories: () => requestJson<StorySummary[]>('/api/stories'),
  interviewStory: (
    conversation: StoryConversationMessage[],
    designBrief: Record<string, unknown>,
  ) =>
    post<StoryInterviewResponse>('/api/stories/interview', {
      conversation,
      design_brief: designBrief,
    }),
  createStoryDraft: (designBrief: Record<string, unknown>) =>
    post<StoryDraftResponse>('/api/stories/drafts', {
      design_brief: designBrief,
    }),
  createStoryGenerationTask: (designBrief: Record<string, unknown>) =>
    post<StoryGenerationTaskResponse>('/api/stories/generation-tasks', {
      design_brief: designBrief,
    }),
  storyGenerationTask: (taskId: string) =>
    requestJson<StoryGenerationTaskResponse>(
      `/api/stories/generation-tasks/${encodeURIComponent(taskId)}`,
    ),
  cancelStoryGenerationTask: (taskId: string) =>
    requestJson<StoryGenerationTaskResponse>(
      `/api/stories/generation-tasks/${encodeURIComponent(taskId)}`,
      { method: 'DELETE' },
    ),
  publishStory: (draftId: string) =>
    post<{ story: StorySummary }>(
      `/api/stories/drafts/${encodeURIComponent(draftId)}/publish`,
      {},
    ),
}

export function roomWebSocketUrl(credential: RoomCredential) {
  const url = new URL(API_BASE_URL, window.location.origin)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  url.pathname = `/ws/rooms/${credential.roomCode}`
  url.search = new URLSearchParams({ token: credential.accessToken }).toString()
  return url.toString()
}
