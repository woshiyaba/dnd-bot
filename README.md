# dnd-bot

一个面向 D&D 跑团的后端原型项目。当前代码里同时存在两条线：

1. **业务主线**：`SessionEngine` 驱动一整局冒险，中央 DM 子图负责对话、检定、剧情推进，战斗子图负责回合制战斗结算。
2. **模板遗留线**：FastAPI 的 `/invoke` 仍是 deepagents + LangGraph 示例入口，用于演示 agent token 通过 WebSocket 流式推送；它还没有接入真正的 D&D 会话主线。

项目的核心原则是：**规则归引擎，叙述归 DM，骰子归玩家**。命中、伤害、HP、先攻、检定成败等确定性规则由 Python 引擎计算；LLM 只负责理解玩家意图、辅助 DM 决策和生成叙述。

## 当前状态

- 已有 FastAPI 服务、WebSocket 推送、deepagents 示例图。
- 已有独立的 D&D 战斗状态机，可中断、可恢复、可复现。
- 已有会话主图，把中央 DM 子图、战斗子图、剧情推进节点串成一局冒险。
- 已有 canon 剧情圣经加载机制，`canon/*.json` 启动时可加载到内存注册表。
- 当前 HTTP 路由尚未暴露 `SessionEngine`，真实会话主要通过测试/驱动脚本调用。

## 项目结构

```text
src/
  app.py                  # FastAPI 应用，目前暴露 /invoke 和 /ws/{user_id}
  graph.py                # deepagents 示例 LangGraph，当前不是 D&D 主流程
  session/                # 一整局冒险的主状态机
    engine.py             # SessionEngine，对外门面
    graph.py              # 会话主图：DM 回合、战斗、剧情推进
    dm_subgraph.py         # 中央 DM 子图：回复、明检定、触发战斗
    story_nodes.py         # canon 剧情推进节点
  combat/                 # 回合制战斗状态机
    engine.py             # CombatEngine，对外门面
    graph.py              # 战斗 LangGraph 装配
    nodes.py              # 战斗节点实现
    rules.py              # 纯规则判断：命中、检定、距离、熟练等
    dice.py               # 可复现骰子
    interrupts.py         # 玩家中断协议与恢复值校验
  dm/                     # DM 智能体、世界桥接、工具与提示词
  model/                  # 领域模型：角色、怪物、战斗状态、DM 状态、枚举
  common/                 # LLM、MySQL、日志、WebSocket、流式 writer 等通用能力
  story/                  # canon 剧情圣经加载与注册表

front/pc-dnd-bot/         # Vite/React 前端
canon/                    # 剧情圣经 JSON
docs/                     # 设计文档与领域规则
knowledge/                # 知识库内容
skills/                   # deepagents skill 文件
test/                     # 可执行流程驱动与冒烟测试
```

## 核心逻辑

### 1. 会话主线

`src.session.engine.SessionEngine` 是当前业务主线入口。一个 engine 实例可以服务多个房间，每个房间使用 `session:{room_id}` 作为 LangGraph `thread_id`。

典型调用顺序：

```python
engine = SessionEngine()

payload = await engine.start_session(room_id, scene_context, opening="我打量四周。")
payload = await engine.message(room_id, "我搜索碎石和石柱。")
payload = await engine.submit(room_id, {"d20": 18})
state = await engine.current_state(room_id)
```

统一返回结构：

- `{"status": "awaiting_input", ...}`：本回合结束，等待玩家下一句话。
- `{"status": "interrupted", "interrupt": {...}, ...}`：等待玩家掷骰或选择行动。
- `{"status": "finished", ...}`：剧情已到结局拍。

会话主图的大致流程：

```text
玩家输入
  ↓
DM 子图 dm_turn
  ↓
route_session
  ├─ wait   → evaluate_advancement → 可能推进 canon beat → 等玩家输入
  └─ combat → run_combat → narrate_aftermath → evaluate_advancement → 等玩家输入
```

### 2. 中央 DM 子图

`src/session/dm_subgraph.py` 负责一个 DM 回合：

```text
perceive
  ↓
dm_decide
  ├─ reply        → narrate_reply
  ├─ player_check → await_roll interrupt → resolve_check → narrate_result
  └─ start_combat → 交回会话主图进入战斗
```

重要边界：

- DM 可以决定“是否需要检定”和“检定什么属性/DC”。
- 玩家明检定的原始 d20 必须通过 `interrupt()` 提交。
- 加值和成败由引擎在 `resolve_check` 中计算。
- `dm_decide` 是独立节点，玩家恢复中断时不会重复调用 LLM。

### 3. 战斗子图

`src.combat.engine.CombatEngine` 可以独立驱动战斗；在会话主图中，战斗作为子图通过 `run_combat` 包装节点嵌入。

战斗流程：

```text
enter_combat
  ↓
judge_surprise
  ↓
roll_initiative
  ↓
next_turn
  ↓
declare_action
  ↓
resolve_action
  ↓
narrate
  ↓
check_end
  ├─ 未结束 → next_turn
  └─ 已结束 → settle → END
```

规则边界：

- 玩家操控角色的先攻、攻击、豁免、属性检定通过中断向玩家要原始 d20。
- 怪物、NPC、环境骰由引擎使用可复现 RNG 自动掷。
- 所有修正值、命中、伤害、HP、战利品结算都在引擎侧完成。
- `rules.py` 不掷骰，只做纯判断；`dice.py` 负责随机；`interrupts.py` 负责协议和信任边界。

### 4. 剧情圣经 canon

`src/story/loader.py` 从 `canon/*.json` 读取剧情圣经，校验后放入进程内注册表。会话状态只保存 `campaign_id`，不把完整 canon 写进 checkpoint。

这样做的目标是：

- canon 可作为只读大对象复用；
- checkpoint 更小；
- 启动时提前发现无法走到结局的剧本结构错误。

## 当前 HTTP 服务

启动服务：

```bash
uv run python main.py
```

实际监听地址：

```text
0.0.0.0:32388
```

当前暴露：

- `GET /ws/{user_id}`：建立 WebSocket，接收流式事件。
- `POST /invoke`：调用 `src/graph.py` 的 deepagents 示例图，返回最终结果，并通过 WebSocket 推送 `flow_start`、节点流式事件和 `flow_end`。

注意：`/invoke` 目前没有使用请求里的 `user_input` 驱动 D&D 会话，也没有调用 `SessionEngine`。后续如果要做真正产品闭环，应新增或替换为 session 相关接口，例如：

- `POST /session/start`
- `POST /session/{room_id}/message`
- `POST /session/{room_id}/submit`
- `GET /session/{room_id}/state`

## 环境配置

复制 `.env.example`：

```bash
copy .env.example .env
```

主服务和 LLM 路径需要：

- `DASHSCOPE_API_KEY`
- `DEFAULT_BASE_URL`
- `DEFAULT_MODEL`（可选，默认由代码决定）
- `MYSQL_HOST`
- `MYSQL_PORT`
- `MYSQL_DATABASE`
- `MYSQL_USER`
- `MYSQL_PASSWORD`
- `PROMPT_CACHE_TTL_SECONDS`

系统提示词从 MySQL 表 `agent_system_prompts` 读取，不从源码文件读取。没有 MySQL 时，主 deepagents 示例入口不可完整使用。

combat/session 的启发式路径可以离线运行；启用 `dm_mode="llm"` 时需要模型环境变量可用。

## 开发命令

安装依赖：

```bash
uv sync
```

启动后端：

```bash
uv run python main.py
```

直接调用 deepagents 示例：

```bash
uv run python -m src.common.example.example_agent
```

运行战斗流程驱动：

```bash
uv run python test/test_combat_flow.py
```

运行会话流程驱动：

```bash
uv run python test/test_session_flow.py
```

运行 pytest 兼容冒烟测试：

```bash
uv run python -m pytest test/test_combat_flow.py
uv run python -m pytest test/test_session_flow.py
```

启动前端：

```bash
cd front/pc-dnd-bot
npm run dev
```

构建前端：

```bash
cd front/pc-dnd-bot
npm run build
```

## 测试注意事项

当前还没有完整测试套件，`test/` 更接近流程驱动：

- `test/test_combat_flow.py`：战斗全流程，可离线跑。
- `test/test_session_flow.py`：中央 DM 到战斗子图的会话流程。
- `test/test_story_flow.py`：交互式 CLI，需要真实模型 key。
- `test/dp.py`：deepagents 示例，导入时可能发起真实模型调用，不要放进常规自动测试。

## 编码约定

- Python 目标版本为 3.13。
- Python 代码使用英文标识符和 PEP 8 命名：函数/变量 `snake_case`，类 `PascalCase`，常量 `UPPER_SNAKE_CASE`。
- 注释、docstring、日志、用户可见叙述使用中文。
- `src/model` 保持纯领域模型，不依赖 LangGraph 或 runtime 层。
- 确定性规则放在 `rules.py`、`dice.py` 等纯模块，不放进 LLM、UI 或 graph glue 里。
- LLM 输出 JSON 使用 `src/common/utils/json_parser.py` 防御式解析。
- enum value 是持久化/前后端传输格式，修改时要同步文档和前端。

## 下一步建议

当前最值得优先推进的是把真实业务主线接到服务入口：

1. 为 `SessionEngine` 增加 HTTP/WebSocket API。
2. 将前端从 `/invoke` 模板入口切到 session start/message/submit/state 流程。
3. 给 `rules.py`、`dice.py`、`interrupts.py`、`CombatEngine` 和 `SessionEngine` 补更细的确定性测试。
4. 清理 `src/app.py`、`src/graph.py` 中过时的模板文案，避免误导后续开发。
