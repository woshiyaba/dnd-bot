import { loadRecentSession } from '../../services/storage'

Page({
  data: {
    recent: null as ReturnType<typeof loadRecentSession>,
    recentTime: '',
    recentStatus: '',
  },
  onShow() {
    const recent = loadRecentSession()
    const statusMap: Record<string, string> = {
      awaiting_input: '等待你的行动',
      interrupted: '有待处理操作',
      finished: '冒险已结束',
    }
    this.setData({
      recent,
      recentTime: recent ? formatTime(recent.updatedAt) : '',
      recentStatus: recent ? statusMap[recent.status] || '进行中' : '',
    })
  },
  continueAdventure() {
    const recent = this.data.recent
    if (!recent) return
    wx.navigateTo({
      url: `/pages/adventure/index?roomId=${encodeURIComponent(recent.roomId)}`,
    })
  },
  openCampaign() {
    wx.navigateTo({ url: '/pages/campaign/index' })
  },
})

function formatTime(timestamp: number): string {
  const date = new Date(timestamp)
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  const hour = String(date.getHours()).padStart(2, '0')
  const minute = String(date.getMinutes()).padStart(2, '0')
  return `${month}-${day} ${hour}:${minute}`
}
