import { sessionStore } from '../../store/session'

Page({
  data: {
    starting: false,
    error: '',
  },
  startAdventure() {
    if (this.data.starting) return
    const roomId = `wx_${Date.now()}_${Math.floor(Math.random() * 10000)}`
    this.setData({ starting: true, error: '' })
    sessionStore
      .start(roomId)
      .then(() => {
        wx.redirectTo({
          url: `/pages/adventure/index?roomId=${encodeURIComponent(roomId)}`,
        })
      })
      .catch((error: unknown) => {
        this.setData({
          starting: false,
          error: error instanceof Error ? error.message : '开局失败',
        })
      })
  },
})
