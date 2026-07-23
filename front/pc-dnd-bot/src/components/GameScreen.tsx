import { useEffect, useMemo, useRef, useState } from 'react'
import type {
  CharacterView,
  DiceType,
  PendingInteraction,
  RoomCredential,
  RoomLobbyView,
  SessionView,
} from '../types/game'
import { CharacterAvatar, CharacterCard } from './CharacterCard'
import { diceTypeForExpression } from '../utils/dice'

type DockTab = 'chat' | 'dice' | 'action' | 'party'

type GameScreenProps = {
  credential: RoomCredential
  lobby: RoomLobbyView | null
  session: SessionView | null
  streamText: string
  isBusy: boolean
  isConnected: boolean
  error: string
  onStart: () => Promise<void>
  onMessage: (content: string) => Promise<void>
  onAction: (action: Record<string, unknown>) => Promise<void>
  onInteractionRoll: (diceType: DiceType, expression: string) => Promise<void>
  onFreeRoll: (diceType: DiceType) => Promise<void>
  onLeave: () => void
}

export function GameScreen(props: GameScreenProps) {
  if (!props.session) {
    return <WaitingRoom {...props} />
  }
  return <AdventureRoom {...props} session={props.session} />
}

function WaitingRoom({
  credential,
  lobby,
  isBusy,
  isConnected,
  error,
  onStart,
  onLeave,
}: GameScreenProps) {
  const currentCharacter = lobby?.characters.find(
    (item) => item.id === credential.member.character_id,
  )
  return (
    <main className="waiting-screen">
      <section className="waiting-card">
        <div className="waiting-heading">
          <span className="rune-small">D20</span>
          <div>
            <p>冒险集结中</p>
            <h1>房间 {credential.roomCode}</h1>
          </div>
          <span className={`connection-pill ${isConnected ? 'connected' : ''}`}>
            {isConnected ? '已连接' : '重连中'}
          </span>
        </div>
        <button
          className="copy-code"
          onClick={() => void navigator.clipboard.writeText(credential.roomCode)}
          type="button"
        >
          复制房间码给同伴
        </button>
        <div className="waiting-party">
          {(lobby?.members ?? [credential.member]).map((member) => {
            const character = lobby?.characters.find(
              (item) => item.id === member.character_id,
            )
            return (
              <article key={member.user_id}>
                <CharacterAvatar
                  name={character?.name ?? member.display_name}
                  color={character?.color ?? '#c9922a'}
                  size="large"
                />
                <strong>{character?.name ?? '冒险者'}</strong>
                <span>
                  {member.display_name}
                  {member.is_host ? ' · 房主' : ''}
                </span>
                <small className={member.is_online ? 'online' : ''}>
                  {member.is_online ? '已入席' : '等待连接'}
                </small>
              </article>
            )
          })}
          {Array.from({
            length: Math.max(0, (lobby?.max_players ?? 6) - (lobby?.members.length ?? 1)),
          }).map((_, index) => (
            <article className="empty-seat" key={`empty-${index}`}>
              <span>+</span>
              <strong>空席</strong>
              <small>等待冒险者</small>
            </article>
          ))}
        </div>
        <div className="waiting-self">
          <span>你将扮演</span>
          <strong>
            {currentCharacter?.name ?? '冒险者'} ·{' '}
            {currentCharacter?.char_class ?? '未知职业'}
          </strong>
        </div>
        {error ? <p className="form-error">{error}</p> : null}
        <div className="waiting-actions">
          <button className="text-button" onClick={onLeave} type="button">
            离开房间
          </button>
          {credential.member.is_host ? (
            <button
              className="primary-cta"
              disabled={isBusy}
              onClick={() => void onStart()}
              type="button"
            >
              {isBusy ? '地下城主正在准备…' : '开始冒险'}
            </button>
          ) : (
            <p>等待房主点亮第一盏火把…</p>
          )}
        </div>
      </section>
    </main>
  )
}

function AdventureRoom({
  credential,
  lobby,
  session,
  streamText,
  isBusy,
  isConnected,
  error,
  onMessage,
  onAction,
  onInteractionRoll,
  onFreeRoll,
  onLeave,
}: GameScreenProps & { session: SessionView }) {
  const [activeTab, setActiveTab] = useState<DockTab>('chat')
  const [input, setInput] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  const timelineRef = useRef<HTMLDivElement>(null)
  const onlineUsers = useMemo(
    () => new Map((lobby?.members ?? []).map((member) => [member.user_id, member])),
    [lobby?.members],
  )
  const party = useMemo(
    () =>
      session.party.map((character) => ({
        ...character,
        is_online:
          onlineUsers.get(character.controller_user_id ?? '')?.is_online ??
          character.is_online,
      })),
    [onlineUsers, session.party],
  )
  const me = party.find((character) => character.is_self) ?? party[0]
  const others = party.filter((character) => !character.is_self)
  const leftPlayers = others.filter((_, index) => index % 2 === 0)
  const rightPlayers = others.filter((_, index) => index % 2 === 1)
  const pending = session.pending_interaction
  const canSubmitCombatText =
    pending?.is_yours === true &&
    pending.interrupt_type === 'declare_action' &&
    pending.options?.natural_language === true
  const composerDisabled =
    isBusy ||
    session.session_status === 'finished' ||
    (Boolean(pending) && !canSubmitCombatText)

  useEffect(() => {
    timelineRef.current?.scrollTo({
      top: timelineRef.current.scrollHeight,
      behavior: 'smooth',
    })
  }, [session.timeline, streamText])

  useEffect(() => {
    if (!pending?.is_yours) return
    setActiveTab(pending.interrupt_type === 'declare_action' ? 'action' : 'dice')
  }, [pending])

  function selectTab(tab: DockTab) {
    setActiveTab(tab)
    if (tab === 'chat') {
      window.setTimeout(() => inputRef.current?.focus(), 50)
    }
  }

  async function send(event: React.FormEvent) {
    event.preventDefault()
    const content = input.trim()
    if (!content || composerDisabled) return
    setInput('')
    if (canSubmitCombatText) {
      await onAction({ action_type: 'natural_language', description: content })
    } else {
      await onMessage(content)
    }
  }

  return (
    <main className="game-shell">
      <header className="game-header">
        <div className="campaign-title">
          <span className="sword-mark">⚔</span>
          <div>
            <strong>暗影峡谷</strong>
            <small>
              {session.scene.round ? `第 ${session.scene.round} 回合` : '钟楼下的低语'} ·{' '}
              {session.scene.phase}
            </small>
          </div>
        </div>
        <div className="room-presence">
          <button
            onClick={() => void navigator.clipboard.writeText(credential.roomCode)}
            type="button"
            title="复制房间码"
          >
            房间 {credential.roomCode}
          </button>
          <span className={isConnected ? 'connected' : ''}>
            <i /> {lobby?.members.filter((member) => member.is_online).length ??
              session.room.online_count}
            /{session.room.member_count} 人在线
          </span>
          <button className="leave-link" onClick={onLeave} type="button">
            离开
          </button>
        </div>
      </header>

      <div className="game-board">
        <PlayerRail players={leftPlayers} />
        <section className="game-center">
          <div className="scene-banner">
            <div className="scene-copy">
              <span>⚔ 地下城主</span>
              <h1>{session.scene.location}</h1>
              <p>{session.scene.description}</p>
            </div>
            <div className="scene-meta">
              {session.scene.threat ? <span>威胁 · {session.scene.threat}</span> : null}
              <span className="ai-badge">✦ AI 驱动</span>
            </div>
            {session.enemies.length ? (
              <div className="enemy-chips">
                {session.enemies.map((enemy) => (
                  <span key={enemy.id}>
                    ☠ {enemy.name} · {enemy.current_hp}/{enemy.max_hp}
                  </span>
                ))}
              </div>
            ) : null}
          </div>

          <section className="story-panel">
            <div className="story-tabs">
              <strong>冒险记录</strong>
              <span>{session.timeline.length} 条记录</span>
            </div>
            <div className="timeline" ref={timelineRef}>
              {session.timeline.length === 0 && !streamText ? (
                <div className="empty-story">
                  <span>✦</span>
                  <strong>火把刚刚点亮</strong>
                  <p>地下城主正在翻开冒险的第一页。</p>
                </div>
              ) : null}
              {session.timeline.map((entry) => (
                <article className={`story-message role-${entry.role}`} key={entry.id}>
                  <span>
                    {entry.role === 'dm'
                      ? '地下城主'
                      : entry.sender_name ?? (entry.role === 'system' ? '系统' : '冒险者')}
                  </span>
                  <p>{entry.content}</p>
                </article>
              ))}
              {streamText ? (
                <article className="story-message role-dm streaming">
                  <span>地下城主</span>
                  <p>{streamText}</p>
                </article>
              ) : null}
            </div>

            {pending ? <WaitingNotice pending={pending} /> : null}
            <form className="chat-composer" onSubmit={send}>
              <span>✦</span>
              <input
                disabled={composerDisabled}
                onChange={(event) => setInput(event.target.value)}
                placeholder={
                  canSubmitCombatText
                    ? '描述你本回合要执行的战斗行动…'
                    : pending
                      ? '当前不是你的行动声明阶段…'
                    : session.session_status === 'finished'
                      ? '这场冒险已经落幕'
                      : '描述你的行动…'
                }
                ref={inputRef}
                value={input}
              />
              <button disabled={!input.trim() || composerDisabled} type="submit">
                发送
              </button>
            </form>
            {error ? <p className="game-error">{error}</p> : null}
          </section>
        </section>
        <PlayerRail players={rightPlayers} />
      </div>

      {activeTab !== 'chat' ? (
        <CommandDrawer
          activeTab={activeTab}
          isBusy={isBusy}
          me={me}
          party={party}
          pending={pending}
          onAction={onAction}
          onClose={() => setActiveTab('chat')}
          onFreeRoll={onFreeRoll}
          onInteractionRoll={onInteractionRoll}
        />
      ) : null}

      {me ? (
        <BottomDock
          activeTab={activeTab}
          me={me}
          pending={pending}
          onSelect={selectTab}
        />
      ) : null}
    </main>
  )
}

function PlayerRail({ players }: { players: CharacterView[] }) {
  return (
    <aside className="player-rail">
      {players.map((player) => (
        <CharacterCard character={player} key={player.id} />
      ))}
      {players.length === 0 ? (
        <div className="rail-empty">
          <span>✦</span>
          <small>等待同伴</small>
        </div>
      ) : null}
    </aside>
  )
}

function WaitingNotice({ pending }: { pending: PendingInteraction }) {
  return (
    <div className={`pending-notice ${pending.is_yours ? 'is-yours' : ''}`}>
      <span>{pending.is_yours ? '轮到你了' : '等待同伴'}</span>
      <p>
        {pending.is_yours
          ? pending.prompt
          : `正在等待 ${pending.directed_to_name ?? '另一位冒险者'} 回应命运。`}
      </p>
    </div>
  )
}

function BottomDock({
  me,
  activeTab,
  pending,
  onSelect,
}: {
  me: CharacterView
  activeTab: DockTab
  pending?: PendingInteraction
  onSelect: (tab: DockTab) => void
}) {
  const hpPercent = Math.max(0, (me.current_hp / Math.max(me.max_hp, 1)) * 100)
  return (
    <footer
      className="bottom-dock"
      style={{ '--character-color': me.color } as React.CSSProperties}
    >
      <div className="self-summary">
        <CharacterAvatar name={me.name} color={me.color} size="large" />
        <div>
          <strong>{me.name}</strong>
          <span>
            Lv.{me.level} {me.char_class}
          </span>
          <small>你 · {me.display_name}</small>
        </div>
      </div>
      <div className="self-hp">
        <span>♥ 生命值</span>
        <strong>
          {me.current_hp}<small> / {me.max_hp}</small>
        </strong>
        <div className="hp-track">
          <i style={{ width: `${Math.min(hpPercent, 100)}%` }} />
        </div>
      </div>
      <div className="self-stats">
        <span><i>◇</i> 护甲 <strong>{me.ac}</strong></span>
        <span><i>⚡</i> 先攻 <strong>{me.current_zone ?? '+0'}</strong></span>
        <span><i>➤</i> 状态 <strong>{me.life_state ?? '正常'}</strong></span>
      </div>
      <nav className="dock-menu" aria-label="角色指令台">
        <DockButton active={activeTab === 'chat'} icon="✦" label="聊天" onClick={() => onSelect('chat')} />
        <DockButton
          active={activeTab === 'dice'}
          badge={pending?.is_yours && pending.interrupt_type !== 'declare_action'}
          icon="◆"
          label="掷骰"
          onClick={() => onSelect('dice')}
        />
        <DockButton
          active={activeTab === 'action'}
          badge={pending?.is_yours && pending.interrupt_type === 'declare_action'}
          icon="⚔"
          label="当前行动"
          onClick={() => onSelect('action')}
        />
        <DockButton active={activeTab === 'party'} icon="♟" label="队伍" onClick={() => onSelect('party')} />
      </nav>
    </footer>
  )
}

function DockButton({
  active,
  badge,
  icon,
  label,
  onClick,
}: {
  active: boolean
  badge?: boolean
  icon: string
  label: string
  onClick: () => void
}) {
  return (
    <button className={active ? 'active' : ''} onClick={onClick} type="button">
      <span>{icon}</span>
      <small>{label}</small>
      {badge ? <i /> : null}
    </button>
  )
}

function CommandDrawer({
  activeTab,
  me,
  party,
  pending,
  isBusy,
  onClose,
  onAction,
  onFreeRoll,
  onInteractionRoll,
}: {
  activeTab: DockTab
  me: CharacterView
  party: CharacterView[]
  pending?: PendingInteraction
  isBusy: boolean
  onClose: () => void
  onAction: (action: Record<string, unknown>) => Promise<void>
  onFreeRoll: (diceType: DiceType) => Promise<void>
  onInteractionRoll: (diceType: DiceType, expression: string) => Promise<void>
}) {
  return (
    <section className="command-drawer">
      <div className="drawer-heading">
        <div>
          <span>{activeTab === 'dice' ? '命运之骰' : activeTab === 'action' ? '行动选择' : '冒险队伍'}</span>
          <strong>
            {activeTab === 'dice'
              ? '选择一颗骰子，结果由服务器裁定'
              : activeTab === 'action'
                ? pending?.prompt ?? '现在没有需要声明的行动'
                : `${party.length} 名冒险者正在同行`}
          </strong>
        </div>
        <button onClick={onClose} type="button" aria-label="关闭面板">×</button>
      </div>
      {activeTab === 'dice' ? (
        <DiceTray
          disabled={isBusy}
          pending={pending}
          onFreeRoll={onFreeRoll}
          onInteractionRoll={onInteractionRoll}
        />
      ) : activeTab === 'action' ? (
        <ActionPanel disabled={isBusy} pending={pending} onAction={onAction} />
      ) : (
        <div className="drawer-party">
          {party.map((character) => (
            <CharacterCard character={character} key={character.id} />
          ))}
        </div>
      )}
      <div className="drawer-owner">当前角色 · {me.name}</div>
    </section>
  )
}

function DiceTray({
  pending,
  disabled,
  onFreeRoll,
  onInteractionRoll,
}: {
  pending?: PendingInteraction
  disabled: boolean
  onFreeRoll: (diceType: DiceType) => Promise<void>
  onInteractionRoll: (diceType: DiceType, expression: string) => Promise<void>
}) {
  const dice: DiceType[] = ['d4', 'd6', 'd8', 'd10', 'd12', 'd20']
  const required = pending?.required_dice
  const requiredType = diceTypeForExpression(required)
  return (
    <div className="dice-tray">
      {pending?.is_yours && pending.interrupt_type !== 'declare_action' ? (
        <button
          className="required-roll"
          disabled={disabled}
          onClick={() => void onInteractionRoll(requiredType, required ?? requiredType)}
          type="button"
        >
          <span>{requiredType.toUpperCase()}</span>
          <div>
            <strong>回应当前检定</strong>
            <small>{pending.prompt} · 引擎加值 {pending.bonus >= 0 ? '+' : ''}{pending.bonus}</small>
          </div>
          <i>立即投掷</i>
        </button>
      ) : null}
      <div className="free-dice">
        <p>自由投掷 · 结果会展示给房间内所有同伴</p>
        <div>
          {dice.map((type) => (
            <button
              disabled={disabled}
              key={type}
              onClick={() => void onFreeRoll(type)}
              type="button"
            >
              {type.toUpperCase()}
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}

function ActionPanel({
  pending,
  disabled,
  onAction,
}: {
  pending?: PendingInteraction
  disabled: boolean
  onAction: (action: Record<string, unknown>) => Promise<void>
}) {
  if (!pending?.is_yours || pending.interrupt_type !== 'declare_action') {
    return <p className="drawer-empty">轮到你的战斗回合时，合法行动会在这里出现。</p>
  }
  const options = pending.options
  return (
    <div className="action-grid">
      {(options?.attack ?? []).flatMap((attack) =>
        (attack.targets ?? []).map((target) => (
          <button
            disabled={disabled}
            key={`${attack.attack_name}-${target.id}`}
            onClick={() =>
              void onAction({
                action_type: 'attack',
                attack_name: attack.attack_name,
                target_id: target.id,
              })
            }
            type="button"
          >
            <span>⚔</span>
            <strong>{attack.attack_name}</strong>
            <small>攻击 {target.name}</small>
          </button>
        )),
      )}
      {(options?.move ?? []).map((move) => (
        <button
          disabled={disabled}
          key={move.target_zone}
          onClick={() =>
            void onAction({ action_type: 'move', target_zone: move.target_zone })
          }
          type="button"
        >
          <span>➤</span>
          <strong>移动</strong>
          <small>前往 {move.target_zone}</small>
        </button>
      ))}
      {(options?.skill ?? []).map((skill) => (
        <button
          disabled={disabled}
          key={skill.skill_id}
          onClick={() =>
            void onAction({ action_type: 'skill', skill_id: skill.skill_id })
          }
          type="button"
        >
          <span>✦</span>
          <strong>技能</strong>
          <small>{skill.skill_id}</small>
        </button>
      ))}
      {(options?.item ?? []).map((item) => (
        <button
          disabled={disabled}
          key={item.item_id}
          onClick={() =>
            void onAction({ action_type: 'item', item_id: item.item_id })
          }
          type="button"
        >
          <span>✚</span>
          <strong>使用物品</strong>
          <small>{item.item_id}</small>
        </button>
      ))}
      {(options?.special ?? []).map((special) => (
        <button
          disabled={disabled}
          key={special.special_action_id}
          onClick={() =>
            void onAction({
              action_type: 'special',
              special_action_id: special.special_action_id,
            })
          }
          type="button"
        >
          <span>✧</span>
          <strong>{special.label}</strong>
          <small>
            {special.description || `对 ${special.target_name} 使用`}
            {special.check
              ? ` · ${special.check.ability} DC ${special.check.dc}`
              : ''}
          </small>
        </button>
      ))}
      {options?.pass ? (
        <button
          className="pass-action"
          disabled={disabled}
          onClick={() => void onAction({ action_type: 'pass' })}
          type="button"
        >
          <span>—</span>
          <strong>结束回合</strong>
          <small>暂不行动</small>
        </button>
      ) : null}
    </div>
  )
}
