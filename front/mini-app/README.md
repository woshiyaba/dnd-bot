# D&D Bot 微信小程序

原生 TypeScript 微信小程序，连接仓库根目录的 FastAPI 会话接口。

## 本地运行

1. 在 `miniprogram/config/env.ts` 中设置 `API_BASE_URL`。
   - 微信开发者工具可使用 `http://127.0.0.1:32388`。
   - 真机调试应改为运行后端电脑的局域网 IP，例如 `http://192.168.1.20:32388`。
   - 发布版本必须使用已在微信公众平台配置的 HTTPS 域名，WebSocket 会自动转换为 WSS。
2. 在本目录运行 `npm install` 和 `npm run type-check`。
3. 使用微信开发者工具导入本目录。默认 `touristappid` 仅用于本地体验，发布前替换为真实 AppID。
4. 在仓库根目录运行 `uv run python main.py` 启动后端。

首版使用固定演示身份 `user_aria`，后端检查点位于进程内存；重启后端会丢失未完成的房间。
