import { sessionStore } from '../../store/session'
import type { Combatant } from '../../types/session'

Page({
  data: {
    character: null as CharacterView | null,
    abilities: [] as AbilityView[],
  },
  onShow() {
    const party = sessionStore.snapshot().payload?.state?.party ?? {}
    const character = Object.values(party)[0]
    if (!character) return
    this.setData({
      character: toCharacterView(character),
      abilities: abilityViews(character),
    })
  },
})

interface CharacterView extends Combatant {
  class_line: string
  hp_percent: number
  proficiency_bonus: number
}

interface AbilityView {
  key: string
  label: string
  score: number
  modifier: string
}

function toCharacterView(character: Combatant): CharacterView {
  const maxHp = Math.max(character.max_hp ?? 1, 1)
  const level = Math.max(character.level ?? 1, 1)
  return {
    ...character,
    class_line: `${character.race || '未知种族'} · ${character.char_class || '冒险者'} ${level}级`,
    hp_percent: Math.round(((character.current_hp ?? maxHp) / maxHp) * 100),
    proficiency_bonus: 2 + Math.floor((level - 1) / 4),
  }
}

function abilityViews(character: Combatant): AbilityView[] {
  const fields: Array<[keyof Combatant, string]> = [
    ['strength', '力量'],
    ['dexterity', '敏捷'],
    ['constitution', '体质'],
    ['intelligence', '智力'],
    ['wisdom', '感知'],
    ['charisma', '魅力'],
  ]
  return fields.map(([key, label]) => {
    const score = Number(character[key] ?? 10)
    const modifier = Math.floor((score - 10) / 2)
    return {
      key: String(key),
      label,
      score,
      modifier: modifier >= 0 ? `+${modifier}` : String(modifier),
    }
  })
}
