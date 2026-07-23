import type { DiceType } from '../types/game'

export function diceTypeForExpression(expression?: string): DiceType {
  const match = expression?.toLowerCase().match(/d(4|6|8|10|12|20)/)
  return (`d${match?.[1] ?? 20}`) as DiceType
}
