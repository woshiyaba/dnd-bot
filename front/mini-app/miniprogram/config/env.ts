/**
 * 后端环境配置。
 *
 * 开发者工具可使用 http://127.0.0.1:32388；真机调试需改为电脑局域网 IP，
 * 发布时必须改为已在微信公众平台登记的 HTTPS 域名。
 */
export const API_BASE_URL = 'http://127.0.0.1:32388'

export const DEFAULT_USER_ID = 'user_aria'
export const DEFAULT_CAMPAIGN_ID = 'whispers_bell_tower'

export function websocketBaseUrl(): string {
  return API_BASE_URL.replace(/^https:/, 'wss:').replace(/^http:/, 'ws:')
}
