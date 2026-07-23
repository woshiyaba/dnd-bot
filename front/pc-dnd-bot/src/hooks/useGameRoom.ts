import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, gameApi, roomWebSocketUrl } from '../api/client'
import type {
  DiceType,
  RollAnimation,
  RoomCredential,
  RoomEvent,
  RoomLobbyView,
  SessionView,
} from '../types/game'

export function useGameRoom(credential: RoomCredential) {
  const [lobby, setLobby] = useState<RoomLobbyView | null>(null)
  const [session, setSession] = useState<SessionView | null>(null)
  const [streamText, setStreamText] = useState('')
  const [rollAnimation, setRollAnimation] = useState<RollAnimation | null>(null)
  const [isBusy, setIsBusy] = useState(false)
  const [isConnected, setIsConnected] = useState(false)
  const [error, setError] = useState('')
  const latestRevision = useRef(0)

  const acceptSession = useCallback((next: SessionView) => {
    if (next.room.revision < latestRevision.current) return
    latestRevision.current = next.room.revision
    setSession(next)
    setStreamText('')
  }, [])

  useEffect(() => {
    let closed = false
    let socket: WebSocket | null = null
    let reconnectTimer: number | undefined
    let heartbeatTimer: number | undefined

    const connect = () => {
      socket = new WebSocket(roomWebSocketUrl(credential))
      socket.onopen = () => {
        setIsConnected(true)
        heartbeatTimer = window.setInterval(() => socket?.send('ping'), 20_000)
      }
      socket.onmessage = (event) => {
        let message: RoomEvent
        try {
          message = JSON.parse(event.data as string) as RoomEvent
        } catch {
          return
        }
        if (message.type === 'room_updated' && message.payload?.room) {
          setLobby(message.payload.room)
          return
        }
        if (message.type === 'session_updated' && message.payload?.session) {
          acceptSession(message.payload.session)
          return
        }
        if (message.type === 'dm_stream_start') {
          setStreamText('')
          return
        }
        if (message.type === 'dm_stream') {
          setStreamText((current) => `${current}${message.payload?.content ?? ''}`)
          return
        }
        if (message.type === 'dice_rolled' && message.payload?.roll) {
          const roll = message.payload.roll
          setRollAnimation((current) => {
            if (current && !current.result && roll.user_id === credential.member.user_id) {
              return { ...current, key: roll.roll_id, result: roll }
            }
            return {
              key: roll.roll_id,
              diceType: roll.dice_type,
              expression: roll.expression,
              result: roll,
            }
          })
        }
      }
      socket.onclose = () => {
        setIsConnected(false)
        if (heartbeatTimer) window.clearInterval(heartbeatTimer)
        if (!closed) reconnectTimer = window.setTimeout(connect, 1600)
      }
      socket.onerror = () => socket?.close()
    }

    void gameApi.lobby(credential.roomCode).then(setLobby).catch(() => undefined)
    void gameApi
      .session(credential)
      .then(acceptSession)
      .catch((reason: unknown) => {
        if (!(reason instanceof ApiError) || reason.status !== 409) {
          setError(reason instanceof Error ? reason.message : '无法恢复房间')
        }
      })
    connect()
    return () => {
      closed = true
      if (reconnectTimer) window.clearTimeout(reconnectTimer)
      if (heartbeatTimer) window.clearInterval(heartbeatTimer)
      socket?.close()
    }
  }, [acceptSession, credential])

  const run = useCallback(async <T,>(request: () => Promise<T>): Promise<T | null> => {
    setIsBusy(true)
    setError('')
    try {
      return await request()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '请求失败')
      return null
    } finally {
      setIsBusy(false)
    }
  }, [])

  const startRoom = useCallback(async () => {
    const next = await run(() => gameApi.start(credential))
    if (next) acceptSession(next)
  }, [acceptSession, credential, run])

  const sendMessage = useCallback(
    async (content: string) => {
      const next = await run(() => gameApi.message(credential, content))
      if (next) acceptSession(next)
    },
    [acceptSession, credential, run],
  )

  const submitAction = useCallback(
    async (action: Record<string, unknown>) => {
      const next = await run(() => gameApi.action(credential, action))
      if (next) acceptSession(next)
    },
    [acceptSession, credential, run],
  )

  const rollInteraction = useCallback(
    async (diceType: DiceType, expression: string) => {
      setRollAnimation({
        key: `pending-${Date.now()}`,
        diceType,
        expression,
      })
      const response = await run(() => gameApi.interactionRoll(credential))
      if (response) {
        setRollAnimation({
          key: response.roll.roll_id,
          diceType: response.roll.dice_type,
          expression: response.roll.expression,
          result: response.roll,
        })
        acceptSession(response.session)
      } else {
        setRollAnimation(null)
      }
    },
    [acceptSession, credential, run],
  )

  const freeRoll = useCallback(
    async (diceType: DiceType) => {
      setRollAnimation({
        key: `pending-${Date.now()}`,
        diceType,
        expression: diceType,
      })
      const roll = await run(() => gameApi.freeRoll(credential, diceType))
      if (roll) {
        setRollAnimation({
          key: roll.roll_id,
          diceType: roll.dice_type,
          expression: roll.expression,
          result: roll,
        })
      } else {
        setRollAnimation(null)
      }
    },
    [credential, run],
  )

  return {
    lobby,
    session,
    streamText,
    rollAnimation,
    isBusy,
    isConnected,
    error,
    startRoom,
    sendMessage,
    submitAction,
    rollInteraction,
    freeRoll,
    dismissRoll: () => setRollAnimation(null),
  }
}
