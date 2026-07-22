Component({
  properties: {
    connection: { type: String, value: 'offline' },
    uiStatus: { type: String, value: 'restoring' },
    error: { type: String, value: '' },
  },
  methods: {
    retry() {
      this.triggerEvent('retry')
    },
  },
})
