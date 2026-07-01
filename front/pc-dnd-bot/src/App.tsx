import { useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import d20Icon from './assets/dice/d20.svg'
import './App.css'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:32388'
const DEFAULT_USER_ID = 'user_aria'

type TimelineMessage = {
  role?: string
  content?: string
}

type Combatant = {
  id?: string
  name?: string
  current_hp?: number
  max_hp?: number
  ac?: number
  life_state?: string
  faction?: string
  current_zone?: string
}

type SceneActor = {
  actor_id?: string
  name?: string
  disposition?: string
  card?: Combatant
}

type SceneState = {
  beat_id?: string
  location?: string
  location_id?: string
  description?: string
  actors?: SceneActor[]
  exits?: string[]
  threat?: string | null
}

type CheckResult = {
  actor_name?: string
  ability?: string
  dc?: number
  d20?: number
  bonus?: number
  total?: number
  success?: boolean
}

type LastCombat = {
  outcome?: string
  granted_loot?: unknown
  casualties?: Array<{ id?: string; name?: string; faction?: string }>
}

type SessionState = {
  messages?: TimelineMessage[]
  scene?: SceneState
  party?: Record<string, Combatant>
  story?: {
    current_beat_id?: string
    delivered_clues?: string[]
    flags?: Record<string, boolean>
  }
  story_status?: string
  last_check?: CheckResult | null
  last_combat?: LastCombat | null
}

type ActionTarget = {
  id: string
  name: string
  zone?: string
}

type ActionOption = {
  attack_name: string
  range: string
  targets?: ActionTarget[]
}

type InterruptRequest = {
  interrupt_type?: string
  prompt?: string
  required_dice?: string
  bonus?: number
  directed_to?: {
    combatant_id?: string
    user_id?: string | null
  }
  options?: {
    attack?: ActionOption[]
    move?: Array<{ target_zone: string }>
    skill?: Array<{ skill_id: string }>
    item?: Array<{ item_id: string; quantity: number }>
  }
}

type SessionPayload = {
  status?: 'awaiting_input' | 'interrupted' | 'finished'
  room_id?: string
  say?: string | null
  interrupt?: InterruptRequest
  last_check?: CheckResult | null
  last_combat?: LastCombat | null
  state?: SessionState
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    const text = await response.text()
    throw new Error(text || `HTTP ${response.status}`)
  }
  return response.json() as Promise<T>
}

function newRoomId() {
  return `demo_${Date.now()}`
}

function rollD20() {
  return Math.floor(Math.random() * 20) + 1
}

function App() {
  const [roomId, setRoomId] = useState(() => localStorage.getItem('roomId') ?? newRoomId())
  const [payload, setPayload] = useState<SessionPayload | null>(null)
  const [input, setInput] = useState('')
  const [manualD20, setManualD20] = useState('18')
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState('')

  const state = payload?.state
  const scene = state?.scene
  const messages = state?.messages ?? []
  const party = useMemo(() => Object.values(state?.party ?? {}), [state?.party])
  const enemies = useMemo(() => hostileActors(scene), [scene])
  const interrupt = payload?.interrupt
  const isFinished = payload?.status === 'finished'

  async function runRequest(request: () => Promise<SessionPayload>) {
    setIsLoading(true)
    setError('')
    try {
      const nextPayload = await request()
      setPayload(nextPayload)
    } catch (err) {
      setError(err instanceof Error ? err.message : '请求失败')
    } finally {
      setIsLoading(false)
    }
  }

  function startSession() {
    const nextRoomId = newRoomId()
    setRoomId(nextRoomId)
    localStorage.setItem('roomId', nextRoomId)
    void runRequest(() =>
      postJson('/session/start', {
        room_id: nextRoomId,
        user_id: DEFAULT_USER_ID,
        campaign_id: 'whispers_bell_tower',
        dm_mode: 'heuristic',
        opening: '我推开破钟酒馆的门，走向村长。',
        random_seed: 20260626,
      }),
    )
  }

  function sendMessage(event: FormEvent) {
    event.preventDefault()
    const userInput = input.trim()
    if (!userInput || !payload || isLoading || interrupt || isFinished) {
      return
    }
    setInput('')
    void runRequest(() =>
      postJson(`/session/${roomId}/message`, {
        user_id: DEFAULT_USER_ID,
        user_input: userInput,
      }),
    )
  }

  function submitResume(resumeValue: Record<string, unknown>) {
    if (!payload || isLoading) {
      return
    }
    void runRequest(() =>
      postJson(`/session/${roomId}/submit`, {
        user_id: DEFAULT_USER_ID,
        resume_value: resumeValue,
      }),
    )
  }

  function submitManualD20() {
    const value = Number.parseInt(manualD20, 10)
    submitResume({ d20: Number.isFinite(value) ? value : 10 })
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">钟楼下的低语</p>
          <h1>D&D 跑团切片</h1>
        </div>
        <div className="topbar-actions">
          <span className={`status-badge status-${payload?.status ?? 'idle'}`}>
            {statusText(payload?.status)}
          </span>
          <button type="button" onClick={startSession} disabled={isLoading}>
            {payload ? '重新开始' : '开始冒险'}
          </button>
        </div>
      </header>

      <section className="scene-strip" aria-label="当前场景">
        <div>
          <span className="label">地点</span>
          <strong>{scene?.location ?? '尚未开局'}</strong>
        </div>
        <div>
          <span className="label">当前拍</span>
          <strong>{state?.story?.current_beat_id ?? '-'}</strong>
        </div>
        <div>
          <span className="label">威胁</span>
          <strong>{scene?.threat ?? '无'}</strong>
        </div>
      </section>

      <section className="content-grid">
        <section className="timeline-panel" aria-label="冒险时间线">
          <div className="panel-heading">
            <h2>冒险记录</h2>
            <span>{messages.length} 条</span>
          </div>
          <div className="timeline">
            {messages.length === 0 ? (
              <div className="empty-state">
                <strong>破钟酒馆的炉火尚未亮起</strong>
                <p>雨声落在窗外，村长还在角落里等候。</p>
              </div>
            ) : (
              messages.map((message, index) => (
                <article
                  className={`message message-${message.role ?? 'system'}`}
                  key={`${message.role}-${index}`}
                >
                  <span>{roleText(message.role)}</span>
                  <p>{message.content}</p>
                </article>
              ))
            )}
          </div>

          {interrupt ? (
            <InterruptPanel
              interrupt={interrupt}
              manualD20={manualD20}
              setManualD20={setManualD20}
              submitManualD20={submitManualD20}
              submitResume={submitResume}
              disabled={isLoading}
            />
          ) : null}

          <form className="input-bar" onSubmit={sendMessage}>
            <input
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder={inputPlaceholder(payload)}
              disabled={!payload || isLoading || Boolean(interrupt) || isFinished}
            />
            <button
              type="submit"
              disabled={!payload || isLoading || Boolean(interrupt) || isFinished}
            >
              发送
            </button>
          </form>
          {error ? <p className="error-line">{error}</p> : null}
        </section>

        <aside className="side-panel" aria-label="状态面板">
          <section className="state-section">
            <h2>场景</h2>
            <p>{scene?.description ?? '开始冒险后显示当前场景。'}</p>
            <div className="tag-list">
              {(scene?.exits ?? []).map((exitName) => (
                <span key={exitName}>{exitName}</span>
              ))}
            </div>
          </section>

          <section className="state-section">
            <h2>队伍</h2>
            <CombatantList combatants={party} emptyText="暂无角色" />
          </section>

          <section className="state-section">
            <h2>在场威胁</h2>
            <CombatantList combatants={enemies} emptyText="暂无敌人" />
          </section>

          <section className="state-section">
            <h2>最近结算</h2>
            <RecentResult
              check={payload?.last_check ?? state?.last_check}
              combat={payload?.last_combat ?? state?.last_combat}
            />
          </section>
        </aside>
      </section>
    </main>
  )
}

function InterruptPanel({
  interrupt,
  manualD20,
  setManualD20,
  submitManualD20,
  submitResume,
  disabled,
}: {
  interrupt: InterruptRequest
  manualD20: string
  setManualD20: (value: string) => void
  submitManualD20: () => void
  submitResume: (resumeValue: Record<string, unknown>) => void
  disabled: boolean
}) {
  const type = interrupt.interrupt_type
  if (type === 'declare_action') {
    return (
      <section className="interrupt-panel">
        <div className="interrupt-title">
          <strong>行动声明</strong>
          <span>{interrupt.directed_to?.combatant_id}</span>
        </div>
        <p>{interrupt.prompt}</p>
        <div className="action-list">
          {(interrupt.options?.attack ?? []).map((weapon) =>
            (weapon.targets ?? []).map((target) => (
              <button
                type="button"
                key={`${weapon.attack_name}-${target.id}`}
                onClick={() =>
                  submitResume({
                    action_type: 'attack',
                    attack_name: weapon.attack_name,
                    target_id: target.id,
                  })
                }
                disabled={disabled}
              >
                攻击 {target.name}
              </button>
            )),
          )}
          {(interrupt.options?.move ?? []).map((move) => (
            <button
              type="button"
              key={move.target_zone}
              onClick={() =>
                submitResume({
                  action_type: 'move',
                  target_zone: move.target_zone,
                })
              }
              disabled={disabled}
            >
              移动到 {move.target_zone}
            </button>
          ))}
          <button
            type="button"
            onClick={() => submitResume({ action_type: 'pass' })}
            disabled={disabled}
          >
            放弃行动
          </button>
        </div>
      </section>
    )
  }

  if (type === 'damage_roll') {
    return (
      <section className="interrupt-panel">
        <div className="interrupt-title">
          <strong>伤害结算</strong>
          <span>{interrupt.required_dice ?? 'damage'}</span>
        </div>
        <p>{interrupt.prompt}</p>
        <button type="button" onClick={() => submitResume({})} disabled={disabled}>
          交给引擎结算
        </button>
      </section>
    )
  }

  return (
    <section className="interrupt-panel">
      <div className="interrupt-title">
        <strong>{interruptTypeText(type)}</strong>
        <span>{interrupt.required_dice ?? 'd20'}</span>
      </div>
      <p>{interrupt.prompt}</p>
      <div className="roll-row">
        <img src={d20Icon} alt="" />
        <input
          type="number"
          min="1"
          max="20"
          value={manualD20}
          onChange={(event) => setManualD20(event.target.value)}
          disabled={disabled}
        />
        <button
          type="button"
          onClick={() => {
            const value = rollD20()
            setManualD20(String(value))
            submitResume({ d20: value })
          }}
          disabled={disabled}
        >
          掷 d20
        </button>
        <button type="button" onClick={submitManualD20} disabled={disabled}>
          提交
        </button>
      </div>
      <span className="bonus-line">引擎加值：{interrupt.bonus ?? 0}</span>
    </section>
  )
}

function CombatantList({
  combatants,
  emptyText,
}: {
  combatants: Combatant[]
  emptyText: string
}) {
  if (combatants.length === 0) {
    return <p className="muted">{emptyText}</p>
  }
  return (
    <div className="combatant-list">
      {combatants.map((combatant) => {
        const maxHp = Math.max(combatant.max_hp ?? 1, 1)
        const hp = Math.max(combatant.current_hp ?? maxHp, 0)
        return (
          <article className="combatant-row" key={combatant.id ?? combatant.name}>
            <div>
              <strong>{combatant.name}</strong>
              <span>AC {combatant.ac ?? '-'}</span>
            </div>
            <div className="hp-line">
              <span style={{ width: `${Math.min((hp / maxHp) * 100, 100)}%` }} />
            </div>
            <small>
              HP {hp}/{maxHp}
            </small>
          </article>
        )
      })}
    </div>
  )
}

function RecentResult({
  check,
  combat,
}: {
  check?: CheckResult | null
  combat?: LastCombat | null
}) {
  if (!check && !combat) {
    return <p className="muted">暂无结算</p>
  }
  return (
    <div className="result-stack">
      {check ? (
        <p>
          检定{check.success ? '成功' : '失败'}：{check.d20}+{check.bonus}=
          {check.total} / DC {check.dc}
        </p>
      ) : null}
      {combat ? (
        <p>
          战斗结果：{combatOutcomeText(combat.outcome)}
          {combat.casualties?.length ? `，倒下 ${combat.casualties.length} 名` : ''}
        </p>
      ) : null}
    </div>
  )
}

function hostileActors(scene?: SceneState): Combatant[] {
  return (scene?.actors ?? [])
    .filter((actor) => actor.disposition === 'hostile')
    .map((actor) => ({
      ...(actor.card ?? {}),
      id: actor.actor_id ?? actor.card?.id,
      name: actor.name ?? actor.card?.name,
    }))
}

function statusText(status?: string) {
  if (status === 'awaiting_input') return '等待玩家'
  if (status === 'interrupted') return '等待掷骰'
  if (status === 'finished') return '已结局'
  return '未开始'
}

function roleText(role?: string) {
  if (role === 'dm') return 'DM'
  if (role === 'user') return '玩家'
  return '系统'
}

function interruptTypeText(type?: string) {
  if (type === 'roll_initiative') return '先攻检定'
  if (type === 'attack_roll') return '攻击检定'
  if (type === 'saving_throw') return '豁免检定'
  if (type === 'ability_check') return '属性检定'
  return '掷骰'
}

function combatOutcomeText(outcome?: string) {
  if (outcome === 'players_win') return '玩家胜利'
  if (outcome === 'players_lose') return '玩家失败'
  return outcome ?? '未知'
}

function inputPlaceholder(payload: SessionPayload | null) {
  if (!payload) return '先开始冒险'
  if (payload.status === 'finished') return '冒险已结束'
  if (payload.status === 'interrupted') return '先处理当前掷骰或行动'
  return '输入你的行动'
}

export default App
