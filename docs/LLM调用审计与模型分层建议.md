# LLM 调用审计与模型分层建议

> 文档状态：阶段 1 模型分层已实施；后续阶段待办继续有效
> 评审基线：2026-07-31
> 评审范围：`src/`、`test/`、两个前端、现有 Canon 与故事生成链
> 第三至第五节的“当前”描述保留为实施前审计基线；第六节起已同步实际配置。

## 一、结论

当前项目的 LLM 边界总体是正确的：LLM 负责理解、裁定计划和叙述，命中、伤害、HP、先攻、检定结果、资源消耗与 Canon 状态写入由确定性引擎负责。主要问题不是“用了太多 LLM 文件”，而是不同风险、不同延迟要求的任务目前被压在两个全局模型配置里：

- 运行时 DM、战斗 DM、语义判定与规则行动编译全部复用 `DEFAULT_MODEL`。
- 故事访谈、完整 Canon 编译与 Canon 修复全部复用 `STORY_GENERATION_MODEL`。
- 这些模型与 Agent 都按子系统整体缓存，不能按“决策 / 叙述 / 封闭分类 / 故事编译”分别选模型。

推荐的总原则是：

1. **中央世界裁定与完整 Canon 创作使用推理模型。**
2. **叙述、封闭选项映射、二值判定、怪物行动等高频窄任务使用快速模型。**
3. **叙述模型必须与工具型决策 Agent 分开。** 纯叙述不应携带骰子、知识库和技能工具。
4. **规则行动编译优先改成确定性模板实例化。** 在改完之前可使用快速模型并保留严格校验。
5. **不能只改 `.env` 中的一个模型名。** 必须建立按角色路由的模型工厂和独立缓存。

本次没有运行需要真实密钥的 live-model flow，因此模型质量和延迟结论来自调用边界、上下文复杂度与失败影响分析。正式切换前仍需用同一批基准输入对快速模型和推理模型做 A/B 评测。

## 二、验证基线

| 检查项 | 结果 | 备注 |
| --- | --- | --- |
| Python 离线单元与合约测试 | 通过 | `87` 个测试通过 |
| PC 端构建 | 通过 | Vite 提示骰子依赖产物约 `545 kB`，需要后续按需加载 |
| PC 端 lint | 通过 | `oxlint` 无报错 |
| 微信小程序 type-check | 未执行 | 本地未安装该目录依赖，系统找不到 `tsc` |
| 真实模型流程 | 未执行 | 未调用外部模型，不包含真实延迟、token 与叙述质量数据 |

阶段 1 实施后，后端离线测试增至 `99` 个并全部通过；另已用临时假密钥完成无网络客户端构造烟测。

测试还暴露了一个依赖维护提示：FastAPI `TestClient` 路径产生 Starlette 关于 `httpx` 的弃用警告，属于低优先级依赖升级事项。

## 三、当前模型装配方式

### 3.1 公共模型工厂

`src/common/utils/llm_util.py`：

- `DEFAULT_MODEL` 的代码缺省值是 `deepseek-v4-pro`。
- `create_chat_model()` 是所有运行时 Agent 的公共入口。
- `MODEL_PRESETS` 已存在，但 `default / analyze / strategy` 当前都是空字典，没有真正承担路由职责。
- `enable_search` 参数当前是空实现，名称会让调用方误以为已经启用了搜索能力。
- 没有按任务显式配置超时、最大输出、采样参数、网络重试和 token 使用采集。

`.env.example` 只展示了 `STORY_GENERATION_MODEL`，没有展示 `DEFAULT_MODEL`。因此未额外配置时，运行时 DM 与故事作者都会落到代码中的 Pro 缺省值。

### 3.2 运行时 DM Agent

`src/dm/agent.py:get_dm_agent()` 创建一个全局缓存 Agent：

```text
DEFAULT_MODEL
  + DM 系统提示词
  + 骰子工具
  + 知识库工具
  + 技能查询工具
  -> 同时服务所有决策与所有叙述
```

这意味着：

- 2～4 句的普通叙述也在使用 Pro 模型。
- 纯叙述也允许模型调用骰子或知识库，可能增加模型往返，甚至消耗全局骰子序列。
- 所有房间共享同一个 Agent 实例和同一组全局工具回调。
- 决策和叙述无法设置不同温度、输出上限、超时或重试策略。

### 3.3 故事生成模型

`src/story/generator.py:_get_model()` 创建一个由 `STORY_GENERATION_MODEL` 控制的全局模型，以下三种任务共用：

- 故事访谈；
- 完整 Canon 编译；
- 校验失败后的完整 Canon 修复。

这三种任务的难度差异很大。访谈通常只是维护设计稿和提出不超过三个问题，不需要每轮都使用 Pro；完整 Canon 编译则涉及大量 ID、引用、剧情连续性和规则契约，必须使用高质量模型。

## 四、所有实际 LLM 使用位置

### 4.1 D&D 产品主链

| 编号 | 入口与调用位置 | 何时调用 | 当前调用次数 | 当前模型 | 建议 |
| --- | --- | --- | --- | --- | --- |
| S1 | `story.generator.continue_interview()` → `_complete_json()` | 每轮故事访谈 | 1 次 | `STORY_GENERATION_MODEL` | Fast |
| S2 | `story.generator.generate_canon()` → `build_canon_authoring_prompt()` | 创建故事草稿 | 1 次 | `STORY_GENERATION_MODEL` | Pro，必须 |
| S3 | `story.generator.generate_canon()` → `build_canon_repair_prompt()` | Canon 校验失败 | 最多 2 次 | `STORY_GENERATION_MODEL` | Pro，当前应保留 |
| D1 | `dm.world_bridge.decide_turn()` → `_decide_llm()` | 每个自然语言世界回合 | 1～3 次，工具调用还会增加内部往返 | `DEFAULT_MODEL` | Pro，必须 |
| D2 | `dm.world_bridge.plan_world_state_guidance()` | 决策连续违反世界写入边界 | 最多 2 次 | `DEFAULT_MODEL` | Fast |
| D3 | `dm.world_bridge.judge_trigger()` | 遇到 `semantic` 推进条件 | 每个待判条件 1 次 | `DEFAULT_MODEL` | Fast |
| D4 | `dm.world_bridge.narrate_turn_final()` | 每个世界回合最终叙述 | 1 次流式调用 | `DEFAULT_MODEL` | Fast、无工具 |
| C1 | `combat.dm_bridge.judge_surprise_llm()` | 每场战斗开始 | 1 次，可能调用骰子/规则工具 | `DEFAULT_MODEL` | Fast |
| C2 | `combat.dm_bridge.narrate_combat_opening_llm()` | 每场战斗开始 | 1 次流式调用 | `DEFAULT_MODEL` | Fast、无工具 |
| C3 | `combat.dm_bridge.decide_action_llm()` | 每个怪物/NPC 行动 | 每次行动 1 次 | `DEFAULT_MODEL` | Fast |
| C4 | `combat.dm_bridge.adjudicate_player_action_llm()` | 玩家用自然语言声明战斗行动 | 每次声明 1 次 | `DEFAULT_MODEL` | Fast |
| C5 | `combat.action_compiler.prepare_action_plan()` | 使用世界或战斗规则行动 | 1～3 次 | `DEFAULT_MODEL` | Fast；中期移除 LLM |
| C6 | `combat.dm_bridge.narrate_llm()` | 每个已结算战斗行动 | 1 次流式调用 | `DEFAULT_MODEL` | Fast、无工具 |

“当前调用次数”只计算应用层调用。`langchain.agents.create_agent` 在使用工具时可能在一次应用调用内再次请求模型，因此 D1、C1、C3 的真实模型往返数可能更高。

### 4.2 已定义但当前主图不走的 LLM helper

下列函数仍会调用模型，但当前会话主图没有直接使用：

- `dm.world_bridge.narrate_reply_llm()`；
- `dm.world_bridge.narrate_result()`；
- `dm.world_bridge.narrate_aftermath()`；
- `dm.world_bridge.narrate_beat_transition()`，只被未装入当前主图的 `story_nodes.narrate_beat()` 间接引用。

保留这些 helper 会增加维护者误判调用量的概率。后续应删除、标记 deprecated，或明确测试覆盖和唯一使用场景。

### 4.3 遗留与测试链

| 位置 | 用途 | 当前情况 | 建议 |
| --- | --- | --- | --- |
| `src/common/example/example_agent.py` | `skills_find` deepagents 示例 | 经 `/invoke` 暴露，使用公共默认模型 | 从产品应用移除；若保留开发接口则用 Fast 并加鉴权 |
| `src/graph.py` | 遗留 LangGraph 示例 | 忽略请求正文并使用硬编码输入、线程 ID | 不应作为产品接口 |
| `src/common/utils/writer.py` | 通用 Agent 流收集器 | 自身不创建模型；`agent_collect()` 会先 `invoke` 再 `astream`，若被使用会重复执行 | 删除未使用函数或修正为单次调用 |
| `test/dp.py` | 多 Agent 演示 | 固定使用 `gemini-3.1-pro-preview`，导入文件就会真实调用 | 移到独立 examples，禁止测试收集或业务导入 |

## 五、模型分层结论

### 5.1 必须使用推理模型的任务

#### A. 中央世界裁定 `decide_turn`

这是运行时最重要的 Pro 调用。它不只是分类，还会同时决定：

- 是否直接回应、要求玩家检定、开始战斗或使用规则行动；
- 检定属性、DC、熟练与成功/失败后的条件化效果；
- Canon 白名单内的世界写入；
- 线索发现、地点移动、跨拍请求和预置遭遇引用；
- 玩家自由表达与当前拍骨架之间的语义一致性。

确定性校验能发现非法 ID 和非法字段，但无法发现“语法合法、语义错误”的裁定。例如玩家明确攻击敌人，模型却给出合法格式的普通回复，这类错误不会被现有 schema 自动识别。因此 D1 应默认使用 Pro，而不是只依靠 Fast 输出通过结构校验。

可通过缩短提示词、预先构造合法候选、减少工具查询和把最终叙述移到 Fast 来控制延迟，不建议直接把 D1 全量降级。

#### B. 长篇故事规划与完整 Canon 编译

完整 Canon 需要同时保证：

- 故事主题、玩家约束和内容边界不漂移；
- Beat、Location、Actor、Trigger、Encounter、Flag、Clue、Item、Action 的引用闭合；
- 分支可达、线索因果、高潮与结局一致；
- 每个持久化效果只有一个原子 owner；
- 战斗卡面与规则行动符合引擎支持范围。

这是典型的全局约束生成任务，应使用 Pro。长篇模式还应先增加一个 Pro 的结构化 StoryPlan 阶段，再分段编译 Canon，不能只把单次输出上限调大。

#### C. 完整 Canon 修复

当前修复会把整个 Canon 草稿重新交给模型。即使错误只涉及一个 ID，模型仍可能改动其它剧情、引用或玩家边界。只要继续采用“整稿重写”，修复就应使用 Pro。

更好的长期方案是确定性修复可机械解决的问题，或让模型只返回受限补丁并重新做完整校验。做到局部补丁后，简单字段修复才适合 Fast。

### 5.2 适合快速模型的任务

以下任务都有一个共同点：输入边界窄、输出短、候选集合封闭，且结果会被引擎再次校验。

- 故事访谈与问题生成；
- `semantic` 是/否条件判断；
- 世界状态冲突后的只读引导计划；
- 怪物从合法攻击/移动/放弃中选动作；
- 玩家自然语言映射到当前合法战斗选项；
- 突袭名单选择；
- 战斗开场、每回合叙述和世界回合最终叙述；
- 当前规则行动模板实例化；
- 遗留技能查找示例。

叙述使用 Fast 不等于降低事实边界。必须继续向模型提供已结算事件，并对空输出、越权新增事实和异常长度做校验。

### 5.3 可以按场景升级到 Pro，但不是默认必需

- 高潮转场、最终结局和重要 NPC 死亡后的特殊叙述；
- 访谈进入最终确认前，对复杂长篇设计稿做一次一致性复核；
- Fast 连续两次无法产生合法封闭决策；
- 玩家输入存在多个合理解释，且不同解释会造成不可逆世界变化。

这些可以作为“质量模式”或失败升级路径，不应让每一条普通战斗叙述都走 Pro。

## 六、推荐的模型角色与配置

当前实现先登记 OpenAI 兼容供应商和复合模型名，再提供默认层级与可选职责覆盖：

```dotenv
# 供应商与模型目录
LLM_PROVIDERS=deepseek
LLM_PROVIDER_DEEPSEEK_BASE_URL=https://api.deepseek.com
LLM_PROVIDER_DEEPSEEK_API_KEY=
LLM_MODELS=deepseek/deepseek-v4-pro,deepseek/deepseek-v4-flash

# 默认物理层
LLM_REASONING_MODEL=deepseek/deepseek-v4-pro
LLM_FAST_MODEL=deepseek/deepseek-v4-flash

# 可选职责覆盖，例如：
DM_NARRATION_MODEL=deepseek/deepseek-v4-flash
```

模型使用 `供应商/上游模型 ID` 复合名。职责变量必须引用 `LLM_MODELS` 中的登记项；完整 DeepSeek
与可选 DashScope/Qwen 配置见 `.env.example`。项目启动时本地校验并构造全部客户端，不发送探测请求。

### 6.1 建议的调用参数

| 职责 | 温度建议 | 输出上限 | 超时策略 | 工具 |
| --- | --- | --- | --- | --- |
| 中央决策 | 低，约 `0～0.2` | 只覆盖决策 JSON | 较短超时，应用层有限纠错 | 按需保留 |
| 二值/封闭决策 | 低，约 `0` | 很小 | 快速失败并有限重试 | 通常无工具 |
| 普通叙述 | 中，约 `0.5～0.7` | 2～4 句对应的小上限 | 关注首 token 超时 | 无工具 |
| 故事访谈 | 中低，约 `0.2～0.4` | 结构化摘要和最多 3 个问题 | 普通请求超时 | 无工具 |
| StoryPlan / Canon | 中，约 `0.3～0.5` | 按阶段设置较大上限 | 独立长任务超时 | 无工具 |
| Canon 修复 | 低，约 `0～0.2` | 受限补丁或完整 Canon | 独立长任务超时 | 无工具 |

部分推理模型不接受或忽略温度参数，实现时应由模型适配层决定，而不是让业务调用方了解厂商差异。

### 6.2 Agent 应按职责拆分

建议至少拆成四类缓存：

```text
DM decision agent
  Pro + KB/skill/dice tools

DM narration model
  Fast + no tools

Combat decision agent
  Fast + only truly required tools

Story models
  Fast interview / Pro plan / Pro author / Pro repair
```

缓存键应包含模型、工具集合、系统提示词版本和关键生成参数，不能继续只缓存一个 `_cached_agent`。

## 七、建议的失败升级策略

### 7.1 结构化快速任务

```text
Fast 首次调用
  -> 严格 schema 校验
  -> 非法时带明确错误纠正 1 次
  -> 仍非法：关键任务升级 Pro；非关键任务显式失败
```

不能使用模板、启发式或默认值伪造 DM 结果。

### 7.2 中央世界裁定

中央裁定直接使用 Pro。格式非法时仍由同一模型在锁定原语义意图后有限纠错。连续失败应显式终止该命令，不提交 checkpoint 中的规则变化。

### 7.3 叙述

Fast 输出必须满足：

- 非空；
- 长度在产品上限内；
- 不包含明显 JSON/字段罗列；
- 不能改写结构化事件中的命中、伤害、HP、死亡或推进结果。

第一次为空或格式异常可再调用一次 Fast；仍失败可以升级 Pro 或显式失败，不能用本地模板补句。

### 7.4 StoryPlan 与 Canon

StoryPlan、Canon 和修复都应保存阶段状态，并区分：

- 网络/超时重试；
- JSON 解析重试；
- schema/引用校验修复；
- 叙事一致性复核。

当前 `_complete_json()` 在 JSON 无法解析时立即结束，尚未进入 Canon 修复循环；访谈 Pydantic 校验失败也没有模型纠错。这两个失败路径需要单独补齐。

## 八、当前主要问题清单

### P0：LLM 路由、成本与质量

| 问题 | 当前证据 | 影响 | 建议 |
| --- | --- | --- | --- |
| 所有运行时任务共用一个 Pro Agent | `dm.agent.get_dm_agent()` 只有一个缓存和 `DEFAULT_MODEL` | 普通叙述、怪物选择等高频任务延迟和成本过高 | 按职责拆模型与缓存 |
| 叙述 Agent 携带全部工具 | `dm_narrate()` 复用同一 `get_dm_agent()` | 可能产生无关工具往返、骰子副作用和中间文本流 | Fast 无工具 narrator |
| 结构化输出校验不一致 | 不同调用方分别手写 `dict` 判断 | `"false"`、错误枚举、未知字段等可能被错误接受 | 每类输出建立严格 Pydantic schema |
| 存在静默修正 | 非法 ability 默认敏捷、非法 kind 默认能力检定、非法 DC 默认 12 | 模型错误被伪装成合法裁定，违反显式失败原则 | 错误反馈给真实 LLM，失败则中止 |
| `judge_trigger` 使用 `bool(value)` | 字符串 `"false"` 会被判为 `True` | 可能错误切换剧情拍 | 强制 `answer` 必须是 JSON boolean |
| 没有模型调用指标 | 未记录 model、token、首 token、总耗时、重试原因 | 无法证明 Flash 是否真的更快且足够好 | 建立统一 LLM gateway 与指标 |
| 没有调用超时和输出预算 | `create_chat_model()` 没有职责级参数 | 请求可能长时间占用房间锁，叙述可能超长 | 按角色设置 timeout/max output |

### P0：故事生成接口与长任务安全

| 问题 | 当前证据 | 影响 | 建议 |
| --- | --- | --- | --- |
| 故事 LLM 接口无鉴权/限流 | `/api/stories/interview`、`/drafts`、`publish` 没有依赖鉴权 | 任意调用者可消耗 Pro 配额、占用内存并发布 Canon | 增加用户身份、配额、并发队列和草稿 ownership |
| 输入体积上限过大且不完整 | 最多 100 条、每条 8000 字；`design_brief` 是无大小限制 dict | 可构造超上下文与成本型 DoS | 限制总字符、字段 schema 和历史摘要 |
| 草稿与发布状态在内存 | `StoryService._drafts` | 重启丢草稿，多实例不一致 | 持久化草稿和状态 |
| 发布写应用本地 `canon/` | `StoryService.publish()` 直接原子写文件 | 容器/多实例环境不可持久、不可同步 | 使用内容存储或数据库并做版本发布 |
| 没有生成并发上限 | 每个请求都可启动 Pro 长任务 | 容易耗尽连接和模型配额 | semaphore/任务队列/用户配额 |

### P0：长局与多人可靠性

这些问题不是 LLM 分层本身，但故事一旦变长就会显著放大：

- `SessionEngine` 默认仍使用 `MemorySaver`，重启后房间与 checkpoint 丢失。
- 房间、在线连接、故事草稿和 Canon 注册表都是进程内状态。
- 房间锁只在单进程内有效；请求没有 `request_id` 或 `expected_revision`，重复请求会被串行执行两次。
- 怪物、环境和 DM 暗骰仍共享进程级 `_ENGINE_DICE`。两个房间交错执行会互相重置和消耗随机序列。
- 访问令牌通过 WebSocket query string 传递，可能进入代理或访问日志。

长篇或多 session 模式上线前，应先完成持久化 checkpointer、房间元数据、幂等命令和 session 级骰子上下文。

### P1：回合延迟与重复调用

典型世界回合：

```text
Pro decide_turn
  -> 可能的 Fast semantic trigger
  -> Fast final narration
```

当前全部是 Pro，普通回合至少两次模型调用；存在 semantic trigger 时至少三次。若世界写入连续冲突，最坏可能出现三次决策纠错、两次引导纠错和一次最终叙述。

典型开战前：

```text
世界 decide_turn
  -> judge_surprise
  -> combat opening narration
  -> 玩家开始掷先攻
```

玩家在看到先攻交互前需要等待三段串行 LLM 工作。建议：

- Canon 已明确 `surprised` 时由引擎直接使用，不再额外问模型；
- 没有有效突袭背景时，避免无意义的突袭 LLM 调用；
- 开场叙述用 Fast 并尽早流式；
- 对关键机械结果之外的装饰叙述，评估是否可以与前端动画并行，但不能在事实提交前提前叙述结果。

怪物回合当前通常是“Fast 决策 + Fast 叙述”两次，这是合理的上限。不要再为普通怪物增加独立规划 Agent。

### P1：长故事上下文和状态增长

- `world_bridge._history_brief()` 只把最近 6 条消息交给 DM，较早的承诺、NPC 关系和未解决支线可能丢失。
- `beat_brief()` 会把所有已发现线索作为 `known_clues` 重新发送；长篇线索增多后，Prompt 会持续膨胀。
- `messages`、`campaign_log` 和 checkpoint 没有归档或分页策略。
- `SessionView.timeline` 每次响应都返回全部消息；长局会让 HTTP/WebSocket payload 越来越大。

应增加由结构化事件派生的长期记忆：

```text
current facts
open threads
resolved threads
NPC relationship states
important promises/choices
current-act recap
```

如果用 Fast 模型生成文字摘要，摘要只能是事件日志的可重建投影，不能成为规则事实的唯一来源。

### P1：故事 Prompt 与确定性校验存在落差

当前 Prompt 要求的部分规则没有对应确定性校验，例如：

- 没有校验目标 Beat 数量和时长密度；
- 没有校验每条路径最终都能到结局，存在循环但不收束的可能；
- 没有校验分支数量、汇流点、线索冗余和 fail-forward；
- 没有校验 `semantic` 条件是否真的是稳定的是/否问题；
- 没有验证内容边界在修复后仍被保留；
- 多个胜利/失败结局的运行时选择语义不完整。

因此，结构校验通过不代表故事足够长、节奏合理或实际可玩。

### P2：遗留与维护问题

- `/invoke` 仍挂在产品 FastAPI 应用上，输入和线程 ID是硬编码，且会触发另一条 LLM 链。
- `src/graph.py` 文件头仍描述不存在的多阶段并行流程。
- `test/dp.py` 是与 D&D 无关的 live-model 演示。
- [已完成] `.env.example` 已提供多供应商目录、Pro/Flash 分层和可选 Qwen 示例。
- [已完成] `dm/agent.py` 已拆成带工具决策 Agent 与无工具流式叙述模型。
- PC 端骰子库被打入超过 500 kB 的独立产物，建议进入游戏或打开骰盘时再动态加载。

## 九、推荐实施顺序

### 阶段 1：先拆模型，不改变业务语义

1. [已完成] 新增多供应商目录与 Fast / Pro 物理模型配置。
2. [已完成] 将 DM 决策、DM 叙述、战斗决策、故事访谈、Canon 创作拆成职责路由和独立缓存。
3. [已完成] 所有普通叙述改用无工具 Fast 模型。
4. [已完成] 保持 `decide_turn` 和 Canon 创作使用 Pro。
5. [待后续可观测性阶段] 记录耗时、首 token、token、重试原因和 session/turn ID。

### 阶段 2：收紧结构化输出

1. 为每类决策增加 `extra="forbid"` 的严格 Pydantic schema。
2. 删除 ability、kind、DC 和 boolean 的静默修正。
3. 为 Fast 封闭任务增加一次纠错和必要的 Pro 升级。
4. 为叙述增加非空与长度校验。
5. 增加超时、并发上限与错误码。

### 阶段 3：减少不必要模型调用

1. 规则行动模板改成确定性实例化。
2. Canon 有明确突袭结果时跳过突袭判断。
3. 限制每拍 `semantic` trigger 数量，优先使用 flag/item/location/combat/action。
4. 删除未使用的叙述 helper 和重复调用工具。
5. 把故事生成改成 StoryPlan → 分段编译 → 完整校验。

### 阶段 4：用数据决定进一步降级

对同一批基准局面同时跑 Fast 和 Pro，只有满足以下条件才将任务稳定降级：

- 结构合法率；
- 正确意图/合法候选选择率；
- Canon 事实一致率；
- 玩家评分；
- P50/P95 首 token 与总耗时；
- 平均重试数和单局 token/费用。

## 十、建议验收指标

| 指标 | 目标方向 |
| --- | --- |
| 普通世界回合模型调用 | 默认 2 次：Pro 决策 + Fast 叙述 |
| 无语义触发的直接按钮行动 | 尽量 0～1 次；机械执行不调用 Pro |
| 怪物普通回合 | 最多 2 次 Fast |
| 结构化 Fast 输出首次合法率 | 持续监控，并以实际基准决定是否可用 |
| LLM 非法输出后的状态提交 | 0 次 |
| 叙述工具调用 | 0 次 |
| 每个模型调用的 model/耗时/token 记录 | 100% |
| 故事创作接口鉴权、限流、ownership | 100% |
| 长局 timeline | 分页或增量，不随每次响应全量增长 |

最终模型选择不应只看一次主观阅读。中央裁定优先保证语义正确，叙述和封闭选择优先保证首 token 与总延迟，Canon 生产优先保证全局一致性和可修复性。
