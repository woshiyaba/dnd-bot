import { useEffect, useState } from 'react'
import d4 from '../assets/dice/d4.svg'
import d6 from '../assets/dice/d6.svg'
import d8 from '../assets/dice/d8.svg'
import d10 from '../assets/dice/d10.svg'
import d12 from '../assets/dice/d12.svg'
import d20 from '../assets/dice/d20.svg'
import type { DiceType, RollAnimation } from '../types/game'

const DICE_ASSETS: Record<DiceType, string> = { d4, d6, d8, d10, d12, d20 }

export function DiceAnimator({
  animation,
  onComplete,
}: {
  animation: RollAnimation
  onComplete: () => void
}) {
  const [settled, setSettled] = useState(false)

  useEffect(() => {
    setSettled(false)
    if (!animation.result) return
    const settleTimer = window.setTimeout(() => setSettled(true), 800)
    const closeTimer = window.setTimeout(onComplete, 2500)
    return () => {
      window.clearTimeout(settleTimer)
      window.clearTimeout(closeTimer)
    }
  }, [animation.key, animation.result, onComplete])

  const rolls = animation.result?.rolls ?? [undefined]
  const isCritical =
    animation.diceType === 'd20' && animation.result?.total === 20
  const isFailure =
    animation.diceType === 'd20' && animation.result?.total === 1

  return (
    <div className="dice-overlay" role="status" aria-live="polite">
      <div
        className={`dice-stage ${settled ? 'is-settled' : 'is-rolling'} ${
          isCritical ? 'is-critical' : ''
        } ${isFailure ? 'is-failure' : ''}`}
      >
        <div className="dice-sprites">
          {rolls.map((value, index) => (
            <div
              className="dice-sprite"
              key={`${animation.key}-${index}`}
              style={{ '--dice-index': index } as React.CSSProperties}
            >
              <img src={DICE_ASSETS[animation.diceType]} alt={animation.diceType} />
              <span>{settled && value !== undefined ? value : '·'}</span>
            </div>
          ))}
        </div>
        <p className="dice-expression">{animation.expression.toUpperCase()}</p>
        {settled && animation.result ? (
          <div className="dice-total">
            <strong>{animation.result.total}</strong>
            <span>
              {isCritical
                ? '命运眷顾 · 大成功'
                : isFailure
                  ? '命运低语 · 大失败'
                  : animation.result.modifier
                    ? `修正 ${animation.result.modifier > 0 ? '+' : ''}${animation.result.modifier}`
                    : `${animation.result.display_name} 的结果`}
            </span>
          </div>
        ) : (
          <p className="dice-waiting">命运正在翻滚…</p>
        )}
      </div>
    </div>
  )
}
