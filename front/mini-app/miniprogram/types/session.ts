export type SessionStatus = 'awaiting_input' | 'interrupted' | 'finished'

export type UiStatus =
  | 'restoring'
  | 'awaiting_input'
  | 'sending'
  | 'streaming'
  | 'awaiting_interaction'
  | 'resolving'
  | 'reconnecting'
  | 'finished'
  | 'error'

export type ConnectionStatus = 'offline' | 'connecting' | 'online'

export interface TimelineMessage {
  role?: 'user' | 'dm' | 'system'
  content?: string
}

export interface Attack {
  name?: string
  attack_bonus?: number
  damage_dice?: string
  damage_type?: string
  range?: string
}

export interface Condition {
  kind?: string
  rounds_left?: number
  amount?: number
}

export interface InventoryItem {
  item_id?: string
  quantity?: number
}

export interface Combatant {
  id?: string
  name?: string
  race?: string | null
  char_class?: string | null
  level?: number
  strength?: number
  dexterity?: number
  constitution?: number
  intelligence?: number
  wisdom?: number
  charisma?: number
  current_hp?: number
  max_hp?: number
  ac?: number
  faction?: string
  life_state?: string
  current_zone?: string
  initiative?: number | null
  attacks?: Attack[]
  conditions?: Array<Condition | string>
  inventory?: InventoryItem[]
  hp_percent?: number
}

export interface SceneActor {
  actor_id?: string
  name?: string
  disposition?: string
  card?: Combatant
}

export interface SceneState {
  beat_id?: string
  location_id?: string
  location?: string
  description?: string
  actors?: SceneActor[]
  exits?: string[]
  threat?: string | null
}

export interface CheckResult {
  actor_name?: string
  ability?: string
  dc?: number
  d20?: number
  bonus?: number
  total?: number
  success?: boolean
  source?: 'manual' | 'virtual'
}

export interface LastCombat {
  outcome?: string
  granted_loot?: unknown
  casualties?: Array<{ id?: string; name?: string; faction?: string }>
  recent_events?: Array<Record<string, unknown>>
}

export interface SessionState {
  user_id?: string
  messages?: TimelineMessage[]
  scene?: SceneState
  party?: Record<string, Combatant>
  story?: {
    visited_count?: number
    clue_count?: number
  }
  story_status?: string
  last_check?: CheckResult | null
  last_combat?: LastCombat | null
  campaign_log?: Array<Record<string, unknown>>
}

export interface ActionTarget {
  id: string
  name: string
  zone?: string
}

export interface AttackOption {
  attack_name: string
  range: string
  targets?: ActionTarget[]
}

export interface CombatView {
  round?: number
  current_actor_id?: string | null
  current_actor_name?: string
  initiative_order?: string[]
  recent_events?: Array<Record<string, unknown>>
  combatants?: Combatant[]
}

export interface InterruptRequest {
  interrupt_type?: string
  prompt?: string
  required_dice?: string | null
  bonus?: number
  directed_to?: { combatant_id?: string; user_id?: string | null }
  options?: {
    attack?: AttackOption[]
    move?: Array<{ target_zone: string }>
    skill?: Array<{ skill_id: string; charges_left?: number }>
    item?: Array<{ item_id: string; quantity?: number }>
    improvise?: boolean
  }
  extra?: {
    damage_dice?: string
    crit?: boolean
    combat?: CombatView
  }
}

export interface RollResult {
  room_id?: string
  interrupt_type?: string
  expression?: string
  rolls?: number[]
  modifier?: number
  total?: number
  source?: 'virtual'
}

export interface SessionPayload {
  status?: SessionStatus
  room_id?: string
  say?: string | null
  interrupt?: InterruptRequest
  last_check?: CheckResult | null
  last_combat?: LastCombat | null
  state?: SessionState
  roll_result?: RollResult
}

export interface SocketMessage {
  type?: string
  room_id?: string
  node?: string
  content?: string
  payload?: SessionPayload | RollResult
}

export interface RecentSession {
  roomId: string
  title: string
  location: string
  status: SessionStatus
  updatedAt: number
}
