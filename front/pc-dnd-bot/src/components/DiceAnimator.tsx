import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type DiceBox from '@3d-dice/dice-box-threejs'
import type { RollAnimation } from '../types/game'

const DICE_HOST_ID = 'dnd-dice-box'
const RESULT_DISPLAY_MS = 2600

const OBSIDIAN_DICE_THEME = {
  name: 'obsidianGold',
  description: '黑曜石骰身与鎏金数字',
  category: 'DND Bot',
  foreground: '#fff0b8',
  background: ['#252b36', '#2d3440', '#202631', '#343b47'],
  outline: '#080a0e',
  texture: 'none',
  material: 'metal' as const,
}

function diceCountForExpression(expression: string) {
  const match = expression.trim().match(/^(\d*)d\d+/i)
  const count = Number(match?.[1] || 1)
  return Math.max(1, Math.min(Number.isFinite(count) ? count : 1, 12))
}

export function DiceAnimator({
  animation,
  onRoll,
  onComplete,
}: {
  animation: RollAnimation | null
  onRoll: () => Promise<void>
  onComplete: () => void
}) {
  const diceBoxRef = useRef<DiceBox | null>(null)
  const initializationRef = useRef<Promise<DiceBox> | null>(null)
  const activeAnimationRef = useRef<RollAnimation | null>(animation)
  const rolledResultKeyRef = useRef('')
  const skipRequestedRef = useRef(false)
  const [engineStatus, setEngineStatus] = useState<'idle' | 'loading' | 'ready' | 'failed'>('idle')
  const [rollError, setRollError] = useState('')
  const [sceneStarted, setSceneStarted] = useState(false)
  const [skipRequested, setSkipRequested] = useState(false)
  const [settled, setSettled] = useState(false)
  const reduceMotion = useMemo(
    () => window.matchMedia('(prefers-reduced-motion: reduce)').matches,
    [],
  )
  const animationKey = animation?.key

  activeAnimationRef.current = animation

  const ensureDiceBox = useCallback(async () => {
    if (diceBoxRef.current) return diceBoxRef.current
    if (initializationRef.current) return initializationRef.current

    setEngineStatus('loading')
    initializationRef.current = (async () => {
      const { default: DiceBoxRenderer } = await import('@3d-dice/dice-box-threejs')
      const instance = new DiceBoxRenderer(`#${DICE_HOST_ID}`, {
        sounds: false,
        shadows: true,
        theme_surface: 'default',
        theme_customColorset: OBSIDIAN_DICE_THEME,
        theme_colorset: 'black',
        theme_texture: 'none',
        theme_material: 'metal',
        color_spotlight: 0xffe8c4,
        light_intensity: 1.5,
        gravity_multiplier: 360,
        strength: 1.25,
      })
      await instance.initialize()
      diceBoxRef.current = instance
      setEngineStatus('ready')
      return instance
    })().catch((error: unknown) => {
      initializationRef.current = null
      setEngineStatus('failed')
      throw error
    })

    return initializationRef.current
  }, [])

  useEffect(() => {
    rolledResultKeyRef.current = ''
    setRollError('')
    setSceneStarted(false)
    skipRequestedRef.current = false
    setSkipRequested(false)
    setSettled(false)
    if (!animationKey) diceBoxRef.current?.clearDice()
  }, [animationKey])

  useEffect(() => {
    if (!animation || animation.phase !== 'rolling' || !animation.result) return

    const resultKey = `${animation.key}:${animation.result.roll_id}`
    if (rolledResultKeyRef.current === resultKey) return
    rolledResultKeyRef.current = resultKey

    if (reduceMotion) {
      setSettled(true)
      return
    }

    let cancelled = false
    const forcedNotation = `${animation.result.rolls.length}${animation.diceType}@${animation.result.rolls.join(',')}`

    void ensureDiceBox()
      .then((diceBox) => {
        if (cancelled || activeAnimationRef.current?.key !== animation.key) return undefined
        setSceneStarted(true)
        const rollPromise = diceBox.roll(forcedNotation)
        if (skipRequestedRef.current) diceBox.simulateThrow()
        return rollPromise
      })
      .then(() => {
        if (cancelled || activeAnimationRef.current?.key !== animation.key) return
        setSettled(true)
      })
      .catch(() => {
        if (cancelled || activeAnimationRef.current?.key !== animation.key) return
        setRollError('3D 骰盘初始化失败，已保留服务器返回的真实点数。')
        setSettled(true)
      })

    return () => {
      cancelled = true
    }
  }, [animation, ensureDiceBox, reduceMotion])

  useEffect(() => {
    if (!settled || !animation?.result) return
    const closeTimer = window.setTimeout(onComplete, RESULT_DISPLAY_MS)
    return () => window.clearTimeout(closeTimer)
  }, [animation?.result, onComplete, settled])

  useEffect(
    () => () => {
      diceBoxRef.current?.clearDice()
    },
    [],
  )

  const rolls = useMemo(
    () =>
      animation?.result?.rolls ??
      Array.from(
        { length: diceCountForExpression(animation?.expression ?? '1d20') },
        () => undefined,
      ),
    [animation?.expression, animation?.result?.rolls],
  )
  const naturalRoll = animation?.result?.rolls.length === 1
    ? animation.result.rolls[0]
    : undefined
  const isCritical = animation?.diceType === 'd20' && naturalRoll === 20
  const isFailure = animation?.diceType === 'd20' && naturalRoll === 1
  const showScene = Boolean(
    animation?.phase === 'rolling' &&
      animation.result &&
      !reduceMotion &&
      !rollError,
  )
  const waitingForServer = animation?.phase === 'rolling' && !animation.result

  function skipOrClose() {
    if (!animation || animation.phase !== 'rolling') return
    if (settled) {
      onComplete()
      return
    }
    if (skipRequestedRef.current) return
    skipRequestedRef.current = true
    setSkipRequested(true)
    if (animation.result && sceneStarted) diceBoxRef.current?.simulateThrow()
  }

  return (
    <div
      aria-hidden={animation ? undefined : true}
      aria-label={
        animation?.phase === 'rolling'
          ? settled
            ? '骰子结果，点击关闭'
            : '骰子正在滚动，点击跳过动画'
          : '准备掷骰'
      }
      aria-live="polite"
      aria-modal={animation ? true : undefined}
      className={`dice-overlay ${animation ? `phase-${animation.phase}` : 'is-dormant'}`}
      onClick={skipOrClose}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          skipOrClose()
        }
      }}
      role={animation ? 'dialog' : undefined}
      tabIndex={animation?.phase === 'rolling' ? 0 : -1}
    >
      <div
        className={`dice-stage ${animation?.phase === 'ready' ? 'is-ready' : 'is-rolling'} ${
          settled ? 'is-settled' : ''
        } ${settled && isCritical ? 'is-critical' : ''} ${
          settled && isFailure ? 'is-failure' : ''
        }`}
      >
        <div
          aria-hidden="true"
          className={`dice-viewport ${showScene ? 'is-visible' : ''}`}
          id={DICE_HOST_ID}
        />

        {animation?.phase === 'ready' ? (
          <>
            {animation.purpose === 'free' ? (
              <button
                aria-label="取消自由掷骰"
                className="dice-cancel"
                onClick={(event) => {
                  event.stopPropagation()
                  onComplete()
                }}
                type="button"
              >
                ×
              </button>
            ) : null}
            <button
              aria-label={`点击投掷 ${animation.diceType.toUpperCase()}`}
              className="dice-roll-trigger"
              onClick={(event) => {
                event.stopPropagation()
                void onRoll()
              }}
              type="button"
            >
              <span aria-hidden="true" className="dice-rune-seal">
                <i />
                <b>{animation.diceType.toUpperCase()}</b>
              </span>
              <strong>点击召唤命运之骰</strong>
            </button>
            <p className="dice-expression">{animation.expression.toUpperCase()}</p>
            {animation.prompt ? <p className="dice-prompt">{animation.prompt}</p> : null}
            {animation.bonus !== undefined ? (
              <span className="dice-bonus">
                引擎加值 {animation.bonus >= 0 ? '+' : ''}{animation.bonus}
              </span>
            ) : null}
          </>
        ) : animation ? (
          <>
            <div className="dice-roll-heading">
              <span>{animation.purpose === 'free' ? '自由投掷' : '命运检定'}</span>
              <strong>{animation.expression.toUpperCase()}</strong>
            </div>
            {settled && animation.result ? (
              <div className="dice-total">
                <div className="dice-roll-breakdown" aria-label="各骰点数">
                  {rolls.map((value, index) => (
                    <span key={`${animation.key}-${index}`}>{value}</span>
                  ))}
                  {animation.result.modifier ? (
                    <em>
                      {animation.result.modifier > 0 ? '+' : ''}
                      {animation.result.modifier}
                    </em>
                  ) : null}
                </div>
                <strong aria-label={`总点数 ${animation.result.total}`}>
                  {animation.result.total}
                </strong>
                <small>
                  {isCritical
                    ? '命运眷顾 · 大成功'
                    : isFailure
                      ? '命运低语 · 大失败'
                      : animation.result.modifier
                        ? `修正 ${animation.result.modifier > 0 ? '+' : ''}${animation.result.modifier}`
                        : `${animation.result.display_name} 的结果`}
                </small>
                {rollError ? <p className="dice-render-error">{rollError}</p> : null}
              </div>
            ) : (
              <div className="dice-cast-status">
                <span aria-hidden="true" className="dice-status-orbit" />
                <p>
                  {waitingForServer
                    ? '正在确认服务器点数…'
                    : skipRequested
                      ? '正在呈现最终点数…'
                      : engineStatus === 'loading' || !sceneStarted
                        ? '正在唤醒 3D 骰盘…'
                        : '命运正在翻滚 · 点击可跳过'}
                </p>
              </div>
            )}
          </>
        ) : null}
      </div>
    </div>
  )
}
