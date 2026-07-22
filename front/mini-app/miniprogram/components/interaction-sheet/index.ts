Component({
  properties: {
    interrupt: { type: Object, value: {} },
    disabled: { type: Boolean, value: false },
    rollResult: { type: Object, value: {} },
  },
  data: {
    manualOpen: false,
    manualValue: '10',
    rollFormula: '',
  },
  observers: {
    interrupt() {
      this.setData({ manualOpen: false, manualValue: '10' })
    },
    rollResult(value) {
      if (!value || value.total === undefined) {
        this.setData({ rollFormula: '' })
        return
      }
      const rolls = Array.isArray(value.rolls) ? value.rolls.join(' + ') : ''
      const modifier = Number(value.modifier || 0)
      const modifierText =
        modifier > 0 ? ` + ${modifier}` : modifier < 0 ? ` - ${-modifier}` : ''
      this.setData({ rollFormula: `${rolls}${modifierText} = ${value.total}` })
    },
  },
  methods: {
    roll() {
      this.triggerEvent('roll')
    },
    toggleManual() {
      this.setData({ manualOpen: !this.data.manualOpen })
    },
    manualInput(event: WechatMiniprogram.Input) {
      this.setData({ manualValue: event.detail.value })
    },
    submitManual() {
      const value = Number(this.data.manualValue)
      const type = (this.properties.interrupt as { interrupt_type?: string })
        .interrupt_type
      this.triggerEvent('manual', { value, damage: type === 'damage_roll' })
    },
    attack(event: WechatMiniprogram.TouchEvent) {
      const { attackName, targetId } = event.currentTarget.dataset
      this.triggerEvent('action', {
        action_type: 'attack',
        attack_name: attackName,
        target_id: targetId,
      })
    },
    move(event: WechatMiniprogram.TouchEvent) {
      this.triggerEvent('action', {
        action_type: 'move',
        target_zone: event.currentTarget.dataset.zone,
      })
    },
    pass() {
      this.triggerEvent('action', { action_type: 'pass' })
    },
  },
})
