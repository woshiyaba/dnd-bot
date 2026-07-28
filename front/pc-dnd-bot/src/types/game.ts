export type DiceType = 'd4' | 'd6' | 'd8' | 'd10' | 'd12' | 'd20'

export type AbilityId =
  | 'strength'
  | 'dexterity'
  | 'constitution'
  | 'intelligence'
  | 'wisdom'
  | 'charisma'

export type CharacterDraft = {
  race_id: string
  class_id: string
  base_abilities: Record<AbilityId, number>
  racial_bonus_choices: AbilityId[]
}

export type CharacterSummary = {
  id: string
  name: string
  race_id: string
  race: string
  class_id: string
  char_class: string
  level: number
  max_hp: number
  ac: number
  initiative: number
  speed: string
  color: string
}

export type CharacterCreationCatalog = {
  abilities: Array<{ id: AbilityId; name: string }>
  point_buy: {
    budget: number
    minimum: number
    maximum: number
    costs: Record<string, number>
  }
  races: Array<{
    id: string
    name: string
    bonuses: Partial<Record<AbilityId, number>>
    choice_count: number
    choice_excludes?: AbilityId[]
    size: string
    speed: string
    proficiencies: string[]
  }>
  classes: Array<{
    id: string
    name: string
    description: string
    hit_die: number
    primary_abilities: AbilityId[]
    save_proficiencies: AbilityId[]
    armor_proficiencies: string[]
    weapon_proficiencies: string[]
    armor: string
    equipment: string[]
    weapon_name: string
    features: Array<{
      id: string
      name: string
      description: string
      unlock_level: number
    }>
    color: string
  }>
}

export type MemberView = {
  user_id: string
  display_name: string
  character_id: string
  character: CharacterSummary
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
  race_id?: string
  char_class?: string
  class_id?: string
  level: number
  experience: number
  next_level_experience?: number | null
  pending_ability_points: number
  abilities: Partial<Record<AbilityId, number>>
  ability_modifiers: Partial<Record<AbilityId, number>>
  skills: Array<{
    skill_id: string
    name: string
    source_type: string
    types: string[]
    charges?: number | null
    cooldown_left: number
    cooldown_rounds: number
  }>
  features: string[]
  current_hp: number
  max_hp: number
  temporary_hp: number
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
    skill?: Array<{
      skill_id: string
      name: string
      source_type: string
      types: string[]
      charges_left?: number | null
      cooldown_left?: number
      min_targets?: number
      max_targets?: number
      targets?: Array<{
        id: string
        name: string
        faction: string
        zone?: string
        life_state: string
      }>
    }>
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
    actions_remaining?: number
    general_actions_remaining?: number
    extra_attacks_remaining?: number
    attack_only?: boolean
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
