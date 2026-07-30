declare module '@3d-dice/dice-box-threejs' {
  type DiceColorSet = {
    name: string
    description?: string
    category?: string
    foreground: string | string[]
    background: string | string[]
    outline: string | string[]
    texture: string | string[]
    material?: 'none' | 'metal' | 'wood' | 'glass' | 'plastic'
  }

  type DiceBoxOptions = {
    sounds?: boolean
    shadows?: boolean
    theme_surface?: 'default' | 'blue-felt' | 'red-felt' | 'green-felt'
    theme_customColorset?: DiceColorSet | null
    theme_colorset?: string
    theme_texture?: string
    theme_material?: 'none' | 'metal' | 'wood' | 'glass' | 'plastic'
    color_spotlight?: number
    light_intensity?: number
    gravity_multiplier?: number
    strength?: number
  }

  export default class DiceBox {
    constructor(elementContainer: string, options?: DiceBoxOptions)

    initialize(): Promise<void>
    roll(notation: string): Promise<unknown>
    simulateThrow(): void
    clearDice(): void
  }
}
