import { sessionStore } from '../../store/session'

Page({
  data: {
    narration: '',
    checkCount: 0,
    visitedCount: 0,
    clueCount: 0,
    outcome: '',
  },
  onShow() {
    const payload = sessionStore.snapshot().payload
    const state = payload?.state
    const messages = state?.messages ?? []
    const narration = [...messages].reverse().find((item) => item.role === 'dm')?.content ?? ''
    const log = state?.campaign_log ?? []
    this.setData({
      narration,
      checkCount: log.filter((item) => item.event === 'ability_check').length,
      visitedCount: state?.story?.visited_count ?? 0,
      clueCount: state?.story?.clue_count ?? 0,
      outcome: outcomeText(payload?.last_combat?.outcome ?? state?.last_combat?.outcome),
    })
  },
  backToAdventure() {
    wx.navigateBack()
  },
  restart() {
    wx.redirectTo({ url: '/pages/campaign/index' })
  },
  backHome() {
    wx.reLaunch({ url: '/pages/home/index' })
  },
})

function outcomeText(outcome?: string): string {
  if (outcome === 'players_win') return '你从战斗中胜出'
  if (outcome === 'players_lose') return '冒险者倒在钟声之下'
  return '故事已经抵达终章'
}
