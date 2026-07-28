import { useEffect, useMemo, useState } from 'react'
import { gameApi } from '../api/client'
import type {
  AbilityId,
  CharacterCreationCatalog,
  CharacterDraft,
  RoomAuthResponse,
  RoomLobbyView,
} from '../types/game'

const ABILITY_ORDER: AbilityId[] = [
  'strength',
  'dexterity',
  'constitution',
  'intelligence',
  'wisdom',
  'charisma',
]

const DEFAULT_ABILITIES: Record<AbilityId, number> = {
  strength: 8,
  dexterity: 8,
  constitution: 8,
  intelligence: 8,
  wisdom: 8,
  charisma: 8,
}

function modifier(score: number) {
  return Math.floor((score - 10) / 2)
}

function signed(value: number) {
  return value >= 0 ? `+${value}` : `${value}`
}

export function LobbyScreen({
  onAuthenticated,
}: {
  onAuthenticated: (response: RoomAuthResponse) => void
}) {
  const [mode, setMode] = useState<'create' | 'join'>('create')
  const [catalog, setCatalog] = useState<CharacterCreationCatalog | null>(null)
  const [remoteLobby, setRemoteLobby] = useState<RoomLobbyView | null>(null)
  const [displayName, setDisplayName] = useState('')
  const [roomCode, setRoomCode] = useState('')
  const [raceId, setRaceId] = useState('')
  const [classId, setClassId] = useState('')
  const [abilities, setAbilities] =
    useState<Record<AbilityId, number>>(DEFAULT_ABILITIES)
  const [racialChoices, setRacialChoices] = useState<AbilityId[]>([])
  const [step, setStep] = useState(0)
  const [isBusy, setIsBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    void gameApi
      .characterOptions()
      .then((result) => {
        setCatalog(result)
        setRaceId(result.races[0]?.id ?? '')
        setClassId(result.classes[0]?.id ?? '')
      })
      .catch((reason: unknown) =>
        setError(reason instanceof Error ? reason.message : '无法加载角色规则'),
      )
  }, [])

  const race = catalog?.races.find((item) => item.id === raceId)
  const characterClass = catalog?.classes.find((item) => item.id === classId)
  const spent = useMemo(() => {
    if (!catalog) return 0
    return ABILITY_ORDER.reduce(
      (total, ability) => total + (catalog.point_buy.costs[String(abilities[ability])] ?? 99),
      0,
    )
  }, [abilities, catalog])
  const remaining = (catalog?.point_buy.budget ?? 27) - spent
  const finalAbilities = useMemo(() => {
    const result = { ...abilities }
    for (const [ability, amount] of Object.entries(race?.bonuses ?? {})) {
      result[ability as AbilityId] += amount ?? 0
    }
    for (const ability of racialChoices) result[ability] += 1
    return result
  }, [abilities, race, racialChoices])

  const preview = useMemo(() => {
    if (!characterClass) return { hp: 1, ac: 10, initiative: 0 }
    const dexterity = modifier(finalAbilities.dexterity)
    const constitution = modifier(finalAbilities.constitution)
    const hp = Math.max(1, characterClass.hit_die + constitution)
    let ac = 18
    if (characterClass.id === 'barbarian') ac = 10 + dexterity + constitution
    else if (characterClass.id === 'bard') ac = 11 + dexterity
    else if (characterClass.id === 'cleric') ac = 16 + Math.min(2, dexterity)
    return { hp, ac, initiative: dexterity }
  }, [characterClass, finalAbilities])

  async function findRoom() {
    const code = roomCode.trim().toUpperCase()
    if (code.length !== 6) return
    setError('')
    try {
      setRemoteLobby(await gameApi.lobby(code))
    } catch (reason) {
      setRemoteLobby(null)
      setError(reason instanceof Error ? reason.message : '没有找到这个房间')
    }
  }

  function chooseRace(nextRaceId: string) {
    setRaceId(nextRaceId)
    setRacialChoices([])
  }

  function chooseClass(nextClassId: string) {
    setClassId(nextClassId)
    setAbilities(DEFAULT_ABILITIES)
  }

  function changeAbility(ability: AbilityId, delta: number) {
    if (!catalog) return
    const next = abilities[ability] + delta
    if (next < catalog.point_buy.minimum || next > catalog.point_buy.maximum) return
    const oldCost = catalog.point_buy.costs[String(abilities[ability])]
    const newCost = catalog.point_buy.costs[String(next)]
    if (delta > 0 && remaining < newCost - oldCost) return
    setAbilities((current) => ({ ...current, [ability]: next }))
  }

  function toggleRacialChoice(ability: AbilityId) {
    if (!race || race.choice_count === 0 || race.choice_excludes?.includes(ability)) return
    setRacialChoices((current) => {
      if (current.includes(ability)) return current.filter((item) => item !== ability)
      if (current.length >= race.choice_count) return current
      return [...current, ability]
    })
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!displayName.trim() || !raceId || !classId || remaining !== 0 || !race) return
    if (racialChoices.length !== race.choice_count) return
    const character: CharacterDraft = {
      race_id: raceId,
      class_id: classId,
      base_abilities: abilities,
      racial_bonus_choices: racialChoices,
    }
    setIsBusy(true)
    setError('')
    try {
      const response =
        mode === 'create'
          ? await gameApi.createRoom(displayName.trim(), character)
          : await gameApi.joinRoom(
              roomCode.trim().toUpperCase(),
              displayName.trim(),
              character,
            )
      onAuthenticated(response)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '进入房间失败')
    } finally {
      setIsBusy(false)
    }
  }

  const canContinueIdentity =
    Boolean(displayName.trim()) && (mode === 'create' || Boolean(remoteLobby))
  const canFinish =
    remaining === 0 && Boolean(race) && racialChoices.length === (race?.choice_count ?? 0)

  return (
    <main className="lobby-screen character-builder-screen">
      <div className="lobby-atmosphere" />
      <section className="lobby-card character-builder-card">
        <div className="lobby-brand">
          <div className="brand-rune">20</div>
          <div>
            <span>FORGE YOUR ADVENTURER</span>
            <h1>铸造冒险者</h1>
            <p>选择血脉与道路，把二十七点潜能化作你的传奇。</p>
          </div>
        </div>

        <div className="lobby-tabs" role="tablist">
          <button
            className={mode === 'create' ? 'active' : ''}
            onClick={() => {
              setMode('create')
              setRemoteLobby(null)
              setStep(0)
              setError('')
            }}
            type="button"
          >
            创建房间
          </button>
          <button
            className={mode === 'join' ? 'active' : ''}
            onClick={() => {
              setMode('join')
              setStep(0)
              setError('')
            }}
            type="button"
          >
            加入队伍
          </button>
        </div>

        <div className="builder-progress" aria-label="角色创建进度">
          {['身份', '种族', '职业', '属性'].map((label, index) => (
            <button
              className={step === index ? 'active' : step > index ? 'done' : ''}
              disabled={index > step}
              key={label}
              onClick={() => setStep(index)}
              type="button"
            >
              <i>{index + 1}</i>
              {label}
            </button>
          ))}
        </div>

        <form onSubmit={submit} className="lobby-form character-builder-form">
          {step === 0 ? (
            <div className="builder-step">
              {mode === 'join' ? (
                <label>
                  <span>房间码</span>
                  <div className="room-code-field">
                    <input
                      value={roomCode}
                      onChange={(event) =>
                        setRoomCode(event.target.value.toUpperCase().slice(0, 6))
                      }
                      onBlur={() => void findRoom()}
                      placeholder="输入 6 位房间码"
                      maxLength={6}
                    />
                    <button type="button" onClick={() => void findRoom()}>
                      查找
                    </button>
                  </div>
                </label>
              ) : null}
              <label>
                <span>冒险者昵称</span>
                <input
                  value={displayName}
                  onChange={(event) => setDisplayName(event.target.value)}
                  placeholder="同伴们如何称呼你？"
                  maxLength={24}
                />
              </label>
              {remoteLobby ? (
                <p className="room-found">
                  已找到房间 · {remoteLobby.members.length}/{remoteLobby.max_players} 名冒险者
                </p>
              ) : null}
            </div>
          ) : null}

          {step === 1 ? (
            <div className="builder-step">
              <div className="step-heading">
                <span>01</span>
                <div><h2>选择种族</h2><p>种族加成会在购点属性之后生效。</p></div>
              </div>
              <div className="builder-option-grid race-grid">
                {(catalog?.races ?? []).map((item) => (
                  <button
                    className={raceId === item.id ? 'selected' : ''}
                    key={item.id}
                    onClick={() => chooseRace(item.id)}
                    type="button"
                  >
                    <strong>{item.name}</strong>
                    <small>
                      {Object.entries(item.bonuses)
                        .map(([ability, amount]) => `${catalog?.abilities.find((a) => a.id === ability)?.name} +${amount}`)
                        .join(' · ')}
                      {item.choice_count ? ` · 自选 ${item.choice_count} 项 +1` : ''}
                    </small>
                    <em>
                      {item.size === 'medium' ? '中型' : item.size} · {item.speed} ·{' '}
                      {item.proficiencies.join('、') || '无额外武器熟练'}
                    </em>
                  </button>
                ))}
              </div>
            </div>
          ) : null}

          {step === 2 ? (
            <div className="builder-step">
              <div className="step-heading">
                <span>02</span>
                <div><h2>选择职业</h2><p>职业决定生命骰、熟练项、装备与成长能力。</p></div>
              </div>
              <div className="builder-option-grid class-grid">
                {(catalog?.classes ?? []).map((item) => (
                  <button
                    className={classId === item.id ? 'selected' : ''}
                    key={item.id}
                    onClick={() => chooseClass(item.id)}
                    style={{ '--character-color': item.color } as React.CSSProperties}
                    type="button"
                  >
                    <strong>{item.name}</strong>
                    <p>{item.description}</p>
                    <small>d{item.hit_die} 生命骰 · {item.armor}</small>
                    <em>
                      特性：{item.features.map((feature) => feature.name).join('、')}
                      {' · '}默认武器：{item.weapon_name}
                    </em>
                    <em title={item.equipment.join('、')}>
                      起始装备：{item.equipment.join('、')}
                    </em>
                  </button>
                ))}
              </div>
            </div>
          ) : null}

          {step === 3 ? (
            <div className="builder-step">
              <div className="step-heading">
                <span>03</span>
                <div><h2>分配属性</h2><p>基础值 8–15；14 与 15 的购点成本更高。</p></div>
                <div className={`point-budget ${remaining === 0 ? 'complete' : ''}`}>
                  <strong>{remaining}</strong><small>剩余点数</small>
                </div>
              </div>
              <div className="ability-builder">
                {catalog?.abilities.map((ability) => {
                  const racialBonus = finalAbilities[ability.id] - abilities[ability.id]
                  const canChooseRace =
                    Boolean(race?.choice_count) &&
                    !race?.choice_excludes?.includes(ability.id)
                  return (
                    <article key={ability.id}>
                      <div>
                        <strong>{ability.name}</strong>
                        <small>{ability.id.toUpperCase()}</small>
                      </div>
                      <button
                        disabled={abilities[ability.id] <= 8}
                        onClick={() => changeAbility(ability.id, -1)}
                        type="button"
                      >−</button>
                      <b>{abilities[ability.id]}</b>
                      <button
                        disabled={abilities[ability.id] >= 15}
                        onClick={() => changeAbility(ability.id, 1)}
                        type="button"
                      >+</button>
                      <div className="ability-final">
                        <strong>{finalAbilities[ability.id]}</strong>
                        <small>{signed(modifier(finalAbilities[ability.id]))}</small>
                      </div>
                      {canChooseRace ? (
                        <button
                          className={`racial-toggle ${racialChoices.includes(ability.id) ? 'selected' : ''}`}
                          onClick={() => toggleRacialChoice(ability.id)}
                          type="button"
                        >
                          {racialChoices.includes(ability.id) ? '种族 +1' : '选择 +1'}
                        </button>
                      ) : racialBonus ? <em>种族 +{racialBonus}</em> : <span />}
                    </article>
                  )
                })}
              </div>
              <aside className="character-preview">
                <div style={{ '--character-color': characterClass?.color } as React.CSSProperties}>
                  <span>{race?.name}</span>
                  <strong>{displayName || '未命名冒险者'}</strong>
                  <small>1 级 {characterClass?.name}</small>
                </div>
                <dl>
                  <div><dt>HP</dt><dd>{preview.hp}</dd></div>
                  <div><dt>AC</dt><dd>{preview.ac}</dd></div>
                  <div><dt>先攻</dt><dd>{signed(preview.initiative)}</dd></div>
                  <div><dt>武器</dt><dd>{characterClass?.weapon_name}</dd></div>
                </dl>
              </aside>
            </div>
          ) : null}

          {error ? <p className="form-error">{error}</p> : null}
          <div className="builder-actions">
            {step > 0 ? <button className="text-button" onClick={() => setStep(step - 1)} type="button">上一步</button> : <span />}
            {step < 3 ? (
              <button
                className="primary-cta"
                disabled={step === 0 && !canContinueIdentity}
                onClick={() => setStep(step + 1)}
                type="button"
              >继续</button>
            ) : (
              <button className="primary-cta" disabled={isBusy || !canFinish} type="submit">
                {isBusy ? '命运正在回应…' : mode === 'create' ? '点亮篝火' : '加入冒险'}
              </button>
            )}
          </div>
        </form>
      </section>
    </main>
  )
}
