export type DiceType = 'd4' | 'd6' | 'd8' | 'd10' | 'd12' | 'd20'

export type CharacterOption = {
  id: string
  name: string
  race: string
  char_class: string
  level: number
  max_hp: number
  ac: number
  initiative: number
  speed: string
  color: string
  available: boolean
}

export type MemberView = {
  user_id: string
  display_name: string
  character_id: string
  is_host: boolean
  is_online: boolean
}

export type RoomLobbyView = {
  room_code: string
  campaign_id: string
  status: 'lobby' | 'playing' | 'finished'
  revision: number
  max_players: number
  members: MemberView[]
  characters: CharacterOption[]
}

export type RoomCredential = {
  roomCode: string
  accessToken: string
  member: MemberView
}

export type RoomAuthResponse = {
  access_token: string
  member: MemberView
  room: RoomLobbyView
}

export type CharacterView = {
  id: string
  name: string
  race?: string
  char_class?: string
  level: number
  current_hp: number
  max_hp: number
  ac: number
  life_state?: string
  conditions: string[]
  current_zone?: string
  controller_user_id?: string
  display_name?: string
  is_self: boolean
  is_online: boolean
  color: string
}

export type TimelineEntry = {
  id: string
  role: 'dm' | 'player' | 'system'
  content: string
  sender_user_id?: string
  sender_name?: string
  character_id?: string
}

export type PendingInteraction = {
  interrupt_type: string
  prompt: string
  required_dice?: string
  bonus: number
  directed_to_user_id?: string
  directed_to_character_id?: string
  directed_to_name?: string
  is_yours: boolean
  options?: {
    attack?: Array<{
      attack_name: string
      range?: string
      targets?: Array<{ id: string; name: string; zone?: string }>
    }>
    move?: Array<{ target_zone: string }>
    skill?: Array<{ skill_id: string; charges_left?: number }>
    item?: Array<{ item_id: string; quantity?: number }>
    special?: Array<{
      special_action_id: string
      label: string
      description?: string
      target_id: string
      target_name: string
      check?: { ability: string; dc: number }
    }>
    natural_language?: boolean
    pass?: boolean
  }
}

export type SessionView = {
  room: {
    room_code: string
    campaign_id: string
    status: 'lobby' | 'playing' | 'finished'
    revision: number
    is_host: boolean
    online_count: number
    member_count: number
  }
  session_status: 'idle' | 'awaiting_input' | 'interrupted' | 'finished'
  scene: {
    location: string
    description: string
    exits: string[]
    threat?: string
    image?: string
    round?: number
    phase?: string
  }
  party: CharacterView[]
  enemies: CharacterView[]
  timeline: TimelineEntry[]
  pending_interaction?: PendingInteraction
  recent_resolution: {
    check?: Record<string, unknown>
    combat?: Record<string, unknown>
  }
}

export type DiceRollResult = {
  roll_id: string
  room_code: string
  purpose: 'free' | 'interaction'
  expression: string
  dice_type: DiceType
  rolls: number[]
  modifier: number
  total: number
  user_id: string
  display_name: string
  character_id: string
  created_at: string
}

export type RollAnimation = {
  key: string
  diceType: DiceType
  expression: string
  result?: DiceRollResult
}

export type RoomEvent = {
  type: string
  room_code: string
  revision: number
  payload?: {
    room?: RoomLobbyView
    session?: SessionView
    roll?: DiceRollResult
    node?: string
    content?: string
  }
}
