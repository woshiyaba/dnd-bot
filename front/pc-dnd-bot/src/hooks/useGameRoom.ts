import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, gameApi, roomWebSocketUrl } from '../api/client'
import type {
  DiceRollResult,
  DiceType,
  RollAnimation,
  RoomCredential,
  RoomEvent,
  RoomLobbyView,
  SessionView,
} from '../types/game'
import { diceTypeForExpression } from '../utils/dice'

export function useGameRoom(credential: RoomCredential) {
  const [lobby, setLobby] = useState<RoomLobbyView | null>(null)
  const [session, setSession] = useState<SessionView | null>(null)
  const [streamText, setStreamText] = useState('')
  const [rollAnimation, setRollAnimation] = useState<RollAnimation | null>(null)
  const [isBusy, setIsBusy] = useState(false)
  const [isDmThinking, setIsDmThinking] = useState(false)
  const [isConnected, setIsConnected] = useState(false)
  const [error, setError] = useState('')
  const latestRevision = useRef(0)
  const promptedInteractionKey = useRef('')
  const rollRequestInFlight = useRef(false)
  const seenRollIds = useRef(new Set<string>())

  const acceptSession = useCallback((next: SessionView) => {
    if (next.room.revision < latestRevision.current) return
    latestRevision.current = next.room.revision
    setSession(next)
    setStreamText('')
    setIsDmThinking(false)
  }, [])

  const showRollResult = useCallback(
    (roll: DiceRollResult) => {
      if (seenRollIds.current.has(roll.roll_id)) return
      seenRollIds.current.add(roll.roll_id)
      if (seenRollIds.current.size > 200) {
        const oldestRollId = seenRollIds.current.values().next().value
        if (oldestRollId) seenRollIds.current.delete(oldestRollId)
      }
      setRollAnimation((current) => {
        const belongsToActiveRoll =
          current?.isOwner === true &&
          current.phase === 'rolling' &&
          roll.user_id === credential.member.user_id &&
          roll.dice_type === current.diceType
        if (belongsToActiveRoll) {
          return { ...current, result: roll }
        }
        if (current) return current
        return {
          key: roll.roll_id,
          diceType: roll.dice_type,
          expression: roll.expression,
          phase: 'rolling',
          purpose: roll.purpose,
          isOwner: false,
          result: roll,
        }
      })
    },
    [credential.member.user_id],
  )

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
          setIsDmThinking(true)
          return
        }
        if (message.type === 'dm_stream') {
          setIsDmThinking(true)
          setStreamText((current) => `${current}${message.payload?.content ?? ''}`)
          return
        }
        if (message.type === 'dm_stream_end') {
          setIsDmThinking(false)
          return
        }
        if (message.type === 'dice_rolled' && message.payload?.roll) {
          showRollResult(message.payload.roll)
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
  }, [acceptSession, credential, showRollResult])

  useEffect(() => {
    if (rollAnimation || !session) return
    const pending = session.pending_interaction
    if (
      !pending?.is_yours ||
      pending.interrupt_type === 'declare_action' ||
      !pending.required_dice
    ) {
      return
    }
    const interactionKey = [
      session.room.revision,
      pending.interrupt_type,
      pending.directed_to_character_id ?? '',
      pending.required_dice,
    ].join(':')
    if (promptedInteractionKey.current === interactionKey) return
    promptedInteractionKey.current = interactionKey
    setRollAnimation({
      key: `interaction-${interactionKey}`,
      diceType: diceTypeForExpression(pending.required_dice),
      expression: pending.required_dice,
      phase: 'ready',
      purpose: 'interaction',
      isOwner: true,
      prompt: pending.prompt,
      bonus: pending.bonus,
    })
  }, [rollAnimation, session])

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

  const runDmRequest = useCallback(
    async <T,>(request: () => Promise<T>): Promise<T | null> => {
      setIsDmThinking(true)
      try {
        return await run(request)
      } finally {
        setIsDmThinking(false)
      }
    },
    [run],
  )

  const startRoom = useCallback(async () => {
    setIsBusy(true)
    setError('')
    try {
      let next: SessionView
      try {
        next = await gameApi.start(credential)
      } catch (reason) {
        if (
          !(reason instanceof ApiError) ||
          reason.status !== 409 ||
          reason.code !== 'player_count_mismatch'
        ) {
          throw reason
        }
        const confirmed = window.confirm(`${reason.message}\n\n是否仍按静态敌人配置开始冒险？`)
        if (!confirmed) return
        next = await gameApi.start(credential, true)
      }
      acceptSession(next)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '开局失败')
    } finally {
      setIsBusy(false)
    }
  }, [acceptSession, credential])

  const sendMessage = useCallback(
    async (content: string) => {
      const next = await runDmRequest(() => gameApi.message(credential, content))
      if (next) acceptSession(next)
    },
    [acceptSession, credential, runDmRequest],
  )

  const submitAction = useCallback(
    async (action: Record<string, unknown>) => {
      const next = await runDmRequest(() => gameApi.action(credential, action))
      if (next) acceptSession(next)
    },
    [acceptSession, credential, runDmRequest],
  )

  const submitLevelUp = useCallback(
    async (increases: Record<string, number>) => {
      const next = await run(() => gameApi.levelUp(credential, increases))
      if (next) acceptSession(next)
    },
    [acceptSession, credential, run],
  )

  const prepareFreeRoll = useCallback((diceType: DiceType) => {
    if (rollRequestInFlight.current) return
    setRollAnimation({
      key: `free-${Date.now()}`,
      diceType,
      expression: diceType,
      phase: 'ready',
      purpose: 'free',
      isOwner: true,
    })
  }, [])

  const startPreparedRoll = useCallback(async () => {
    if (
      !rollAnimation ||
      rollAnimation.phase !== 'ready' ||
      !rollAnimation.isOwner ||
      rollRequestInFlight.current
    ) {
      return
    }
    const preparedRoll = rollAnimation
    rollRequestInFlight.current = true
    setRollAnimation({ ...preparedRoll, phase: 'rolling' })
    try {
      if (preparedRoll.purpose === 'free') {
        const roll = await run(() => gameApi.freeRoll(credential, preparedRoll.diceType))
        if (roll) {
          showRollResult(roll)
        } else {
          setRollAnimation((current) =>
            current?.key === preparedRoll.key ? null : current,
          )
        }
        return
      }

      const response = await runDmRequest(() => gameApi.interactionRoll(credential))
      if (response) {
        showRollResult(response.roll)
        acceptSession(response.session)
      } else {
        promptedInteractionKey.current = ''
        setRollAnimation((current) =>
          current?.key === preparedRoll.key && !current.result ? null : current,
        )
      }
    } finally {
      rollRequestInFlight.current = false
    }
  }, [acceptSession, credential, rollAnimation, run, runDmRequest, showRollResult])

  const dismissRoll = useCallback(() => setRollAnimation(null), [])

  return {
    lobby,
    session,
    streamText,
    rollAnimation,
    isBusy,
    isDmThinking,
    isConnected,
    error,
    startRoom,
    sendMessage,
    submitAction,
    submitLevelUp,
    prepareFreeRoll,
    startPreparedRoll,
    dismissRoll,
  }
}
