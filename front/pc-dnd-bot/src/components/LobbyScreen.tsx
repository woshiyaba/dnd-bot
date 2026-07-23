import { useEffect, useMemo, useState } from 'react'
import { gameApi } from '../api/client'
import type {
  CharacterOption,
  RoomAuthResponse,
  RoomLobbyView,
} from '../types/game'

export function LobbyScreen({
  onAuthenticated,
}: {
  onAuthenticated: (response: RoomAuthResponse) => void
}) {
  const [mode, setMode] = useState<'create' | 'join'>('create')
  const [characters, setCharacters] = useState<CharacterOption[]>([])
  const [remoteLobby, setRemoteLobby] = useState<RoomLobbyView | null>(null)
  const [displayName, setDisplayName] = useState('')
  const [roomCode, setRoomCode] = useState('')
  const [characterId, setCharacterId] = useState('')
  const [isBusy, setIsBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    void gameApi
      .characters()
      .then((items) => {
        setCharacters(items)
        setCharacterId(items[0]?.id ?? '')
      })
      .catch((reason: unknown) =>
        setError(reason instanceof Error ? reason.message : '无法加载角色'),
      )
  }, [])

  const visibleCharacters = useMemo(
    () => remoteLobby?.characters ?? characters,
    [characters, remoteLobby],
  )

  async function findRoom() {
    const code = roomCode.trim().toUpperCase()
    if (code.length !== 6) return
    setError('')
    try {
      const lobby = await gameApi.lobby(code)
      setRemoteLobby(lobby)
      const firstAvailable = lobby.characters.find((item) => item.available)
      setCharacterId(firstAvailable?.id ?? '')
    } catch (reason) {
      setRemoteLobby(null)
      setError(reason instanceof Error ? reason.message : '没有找到这个房间')
    }
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!displayName.trim() || !characterId) return
    setIsBusy(true)
    setError('')
    try {
      const response =
        mode === 'create'
          ? await gameApi.createRoom(displayName.trim(), characterId)
          : await gameApi.joinRoom(
              roomCode.trim().toUpperCase(),
              displayName.trim(),
              characterId,
            )
      onAuthenticated(response)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '进入房间失败')
    } finally {
      setIsBusy(false)
    }
  }

  return (
    <main className="lobby-screen">
      <div className="lobby-atmosphere" />
      <section className="lobby-card">
        <div className="lobby-brand">
          <div className="brand-rune">20</div>
          <div>
            <span>AI DUNGEON MASTER</span>
            <h1>暗影峡谷</h1>
            <p>与同伴围坐于命运之桌，开启一场只属于你们的冒险。</p>
          </div>
        </div>

        <div className="lobby-tabs" role="tablist">
          <button
            className={mode === 'create' ? 'active' : ''}
            onClick={() => {
              setMode('create')
              setRemoteLobby(null)
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
              setError('')
            }}
            type="button"
          >
            加入队伍
          </button>
        </div>

        <form onSubmit={submit} className="lobby-form">
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

          <fieldset disabled={mode === 'join' && !remoteLobby}>
            <legend>选择你的角色</legend>
            <div className="character-options">
              {visibleCharacters.map((character) => (
                <button
                  className={characterId === character.id ? 'selected' : ''}
                  disabled={!character.available}
                  key={character.id}
                  onClick={() => setCharacterId(character.id)}
                  style={
                    { '--character-color': character.color } as React.CSSProperties
                  }
                  type="button"
                >
                  <strong>{character.name}</strong>
                  <span>{character.char_class}</span>
                  <small>
                    HP {character.max_hp} · AC {character.ac}
                  </small>
                  {!character.available ? <em>已被选择</em> : null}
                </button>
              ))}
            </div>
          </fieldset>

          {remoteLobby ? (
            <p className="room-found">
              已找到房间 · {remoteLobby.members.length}/{remoteLobby.max_players}{' '}
              名冒险者
            </p>
          ) : null}
          {error ? <p className="form-error">{error}</p> : null}
          <button
            className="primary-cta"
            disabled={
              isBusy ||
              !displayName.trim() ||
              !characterId ||
              (mode === 'join' && !remoteLobby)
            }
            type="submit"
          >
            {isBusy ? '命运正在回应…' : mode === 'create' ? '点亮篝火' : '加入冒险'}
          </button>
        </form>
      </section>
    </main>
  )
}
