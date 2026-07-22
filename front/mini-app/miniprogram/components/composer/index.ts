Component({
  properties: {
    value: { type: String, value: '' },
    disabled: { type: Boolean, value: true },
    placeholder: { type: String, value: '描述你的行动…' },
  },
  data: {
    hasValue: false,
  },
  observers: {
    value(value: string) {
      this.setData({ hasValue: Boolean(value.trim()) })
    },
  },
  methods: {
    input(event: WechatMiniprogram.Input) {
      this.triggerEvent('change', { value: event.detail.value })
    },
    send() {
      if (!this.properties.disabled) this.triggerEvent('send')
    },
    voiceHint() {
      wx.showToast({ title: '语音输入将在后续版本开放', icon: 'none' })
    },
  },
})
