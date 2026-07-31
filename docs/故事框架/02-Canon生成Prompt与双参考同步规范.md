# Canon 生成 Prompt 与双参考同步规范

> 基线日期：2026-07-29  
> 适用模块：故事访谈、Canon 编译、Canon 修复与发布前校验  
> 参考剧本：`canon/prodigal_return_quest.json`、`canon/whispers_bell_tower.json`

## 一、目标与事实源

故事生成不是让模型自由写一篇剧情，而是把玩家已经确认的 `design_brief` 编译成引擎能够
反序列化、校验和运行的 Canon JSON。当前链路应保持为：

```text
玩家构思
  → 故事策划访谈
  → 玩家确认 design_brief
  → Canon 编译 Prompt
     ├─ 当前字段与规则契约
     ├─ 两份实时读取的内置 Canon
     └─ 已占用 campaign_id
  → Canon JSON 草稿
  → Canon.from_dict
  → validate_canon + validate_authored_canon
     ├─ 成功：形成可发布草稿
     └─ 失败：携带确定性错误最多修复两次
```

不同信息的唯一事实源如下：

| 信息 | 唯一事实源 | 说明 |
|---|---|---|
| Canon 数据形状与默认值 | `src/model/canon.py`、`src/model/rule_action.py` | JSON 能否被引擎解析，以这里为准 |
| 引用闭合、可达性、唯一原子入口 | `validate_canon`、`validate_authored_canon` | Prompt 不能代替确定性校验 |
| 访谈、编译与修复约束 | `src/story/prompt.py` | 这是实际发送给模型的可执行 Prompt |
| 当前合法结构范例 | 两份 `canon/*.json` | 每次编译重新读盘，不在 Python 中复制一份静态快照 |
| 故事生成模型 | `STORY_INTERVIEW_MODEL`、`STORY_AUTHORING_MODEL`、`STORY_REPAIR_MODEL` | 从中央模型目录按职责选择 |

文档用于说明契约和维护顺序，不应成为第三份可执行 Schema。当文档、Prompt、样例与模型代码
不一致时，先按模型和校验器修复实现，再同步本文。

## 二、当前 Prompt 分层

### 1. 故事策划访谈 Prompt

访谈 Prompt 由 `STORY_INTERVIEW_RULE`、完整对话历史和上一版 `design_brief` 组成，只负责确认
会改变体验的大方向：

- 玩家身份、动机与事件关系。
- 核心冲突与主要对手方向。
- 调查、社交、探索、战斗、解谜的玩法优先级。
- 基调、内容边界、时长、人数和结局方向。
- 必须保留、必须避开的元素，以及明确授权系统自行决定的范围。

它必须输出结构化 JSON，状态只能是 `needs_clarification`、`ready_for_confirmation` 或
`confirmed`。每轮最多问 3 个问题；第一次输入再完整，也不能跳过玩家确认直接生成 Canon。
只有以下门槛全部成立，才能进入编译：

```text
design_brief.user_confirmed == true
design_brief.confirmed_revision == design_brief.revision
validate_confirmed_design_brief(design_brief) == []
```

### 2. Canon 编译 Prompt

一次编译请求由四段组成：

1. `CANON_AUTHORING_RULE`：角色、合法字段、枚举、引用和机械效果的硬约束。
2. `<confirmed_design_brief>`：玩家已确认的唯一创作方向。
3. `<reference_canons>`：每次请求重新读取的两份完整 Canon JSON。
4. `<reserved_campaign_ids>`：`canon/` 目录中所有已占用的剧本 ID。

两份参考 Canon 只证明“这种字段组织和引用方式能被当前引擎接受”，不能覆盖 Prompt 的硬规则，
也不能成为复制人物、地点、谜底、数值或专有名词的素材库。若参考内容与模型或校验器发生冲突，
以模型和校验器为准，并立即修正参考 Canon。

编译器的核心任务描述可以归纳为：

```text
你是 D&D 短篇冒险的 Canon 编译器，不是运行时 DM。
只把玩家明确确认的 design_brief 编译为一个完整 JSON 对象。
使用当前引擎已经实现的字段、枚举和封闭效果；所有 ID 与引用必须闭合。
Canon 定义世界事实和规则入口，DM 负责叙述，引擎负责状态与结算。
不得从参考 Canon 复制剧情内容，不得以 loot_table 代替背包物品发放，
不得让同一 flag 或 item_id 拥有多个原子写入入口。
提交前检查起始拍、胜败结局、拍可达性、固定战斗卡、出口、线索、遭遇和行动定义。
最终只输出 JSON，不要 Markdown、解释、注释或代码围栏。
```

### 3. Canon 修复 Prompt

修复 Prompt 只接收上一份完整草稿和确定性校验错误。模型可以修复字段、引用、所有权与可达性，
但不能趁修复改变玩家确认的主题、角色关系或核心冲突。修复后仍返回完整 Canon JSON，并再次走
相同的两个校验器；最多修复两次，之后显式失败，不生成离线或模板故事兜底。

## 三、两份 Canon 的互补结构

两份剧本都采用 5 拍短篇骨架：`opening → exploration → climax → ending_win / ending_lose`。
`Beat.kind` 描述剧情功能，不限制探索拍内出现预置遭遇，因此《浪子归乡》的探索拍可以包含探子战斗。

| 对比项 | `whispers_bell_tower.json` | `prodigal_return_quest.json` | 生成 Prompt 应学到的规则 |
|---|---|---|---|
| 主要体验 | 三地点调查，线索强化 Boss 战 | 山门探索、探子战斗、任务道具开门 | 同一骨架可以承载不同玩法组合 |
| 探索空间 | 钟楼前厅、枯井、墓园互通 | 残破石门、铭文厅互通 | 一拍可包含多个 `location_ids`，`intra_exits` 必须闭合 |
| 进入高潮 | `ascend_tower` 行动即可上楼 | 铜钥匙开门或暴力破门 | 普通路线不应无故成为空气墙；真实硬门槛要有世界依据和补救路线 |
| 环境线索 | 钟裂、真名、圣水 | 火蟒弱点、寒冰浮雕与符箓 | 环境内容只有在玩家调查、交互或检定后才发现 |
| 战后随身物 | 无 | 探子密信与铜钥匙 | 敌人被击败后可无障碍取得的内容放入同拍 `on_win_discoveries` |
| 关键物品 | 圣水 | 铜钥匙、寒冰符箓 | 真实背包物品只由唯一 `KeyInfo.discovery_effects.grant_items` 发放 |
| 世界规则行动 | 无 | `use_copper_key` 推进到高潮拍 | 跨拍效果必须限定来源拍，并指向该拍合法的 action 出口 |
| 战斗规则行动 | 钟裂、真名、圣水 | 风老震慑、寒冰符箓 | 线索或物品若有机械收益，必须由 `ActionDefinition` 封闭表达 |
| Boss 结算 | 绑定 `boss_bell_spirit` | 绑定 `boss_vermillion_serpent` | 全局胜利条件应绑定指定遭遇，避免击败无关敌人误通关 |
| 战利品摘要 | 银铃、残余圣水、碎银 | 龙血玉、火蟒鳞片、古玄晶 | `loot_table` 只展示摘要；主线物品不能靠它入库 |
| 卡关兜底 | NPC 主动给未发现线索并指路 | 提示铜钥匙或暴力破门 | 兜底要保持世界内合理，不能凭空提交发现或规则效果 |

这两份参考不能被压缩成单一模板。钟楼剧本证明“零线索仍可进入高潮、调查只提供优势”；浪子
剧本证明“有明确门锁时可以设置硬门槛，但要提供钥匙与替代行动”，并覆盖战后自动搜获的边界。

## 四、Canon 编译字段清单

### 1. 顶层 Canon

生成结果应显式给出以下字段：

```json
{
  "campaign_id": "unique_snake_case_id",
  "title": "玩家可见标题",
  "premise": "一句话主线",
  "theme": "主题",
  "tone": "基调",
  "duration_minutes": 20,
  "recommended_player_count": 1,
  "gameplay_focus": ["调查", "探索", "战斗"],
  "content_warnings": [],
  "declared_flags": [],
  "action_definitions": [],
  "win_condition": {},
  "lose_condition": {},
  "cast": [],
  "locations": [],
  "start_beat_id": "opening_beat",
  "beats": []
}
```

- 时长只能是 10～30 分钟，推荐人数只能是 1～6。
- `campaign_id` 不能与已发布 Canon 重复。
- 所有技术 ID 使用小写 ASCII `snake_case`，同类 ID 唯一。
- `declared_flags` 必须覆盖线索、遭遇、规则行动和受限世界状态可能写入的全部 flag。
- 标题、前提、主题、基调、玩法侧重和内容提示会公开，不得泄露谜底、NPC 秘密或结局。

### 2. NPC 与战斗卡

`NpcSpec` 包含 `id`、`name`、`role`、`goal`、`secret`、`disposition`、可选 `card`，以及
`story_critical` 与 `death_fallback`。凡是可能被攻击或进入遭遇的 actor 都要有固定卡面，且
`card.id == NpcSpec.id`。关键 NPC 死亡可能阻断主线时，必须提供非空的 `guidance` 与
`consequence`；死亡本身不能自动授予线索、物品或推进状态。

战斗卡至少要固定六项属性、当前/最大 HP、AC、先攻加值和攻击列表。命中、伤害、HP 与胜负由
引擎结算，Canon 编译器和运行时 DM 都不能临场改算。

### 3. 地点与剧情拍

`LocationSpec` 包含 `id`、`name`、`description` 和 `intra_exits`。`intra_exits` 只能引用现有
地点；需要往返时写成双向连接。

每个 `Beat` 包含：

- `id`、`title`、`kind`、`location_ids`。
- `entry_state`：进入地点、场景事实、在场 actor、可理解出口和威胁。
- `key_info`：本拍可能发现的受控线索。
- `advance_conditions` 与 `exits`：触发器到下一拍的确定映射。
- `stuck_fallback`：玩家空转时的自然提示。
- 可选 `encounter`；结局拍使用 `ending_outcome`。

非结局拍至少一个出口，结局拍不再推进。失败结局若要保留真实失败地点，应使用
`entry_state.preserve_current_scene=true` 和 `location_id=null`。

### 4. Trigger 与出口

合法 `Trigger.kind` 及 predicate 为：

| kind | predicate | 结算者 |
|---|---|---|
| `flag` | `{"flag":"id","equals":true}`、`all` 或 `any` | 引擎 |
| `item` | `{"item_id":"id"}` | 引擎读取全队背包 |
| `location` | `{"location_id":"id"}` | 引擎 |
| `combat_outcome` | `{"outcome":"players_win","encounter_id":"id"}` | 引擎 |
| `semantic` | `{"prompt":"只能回答是或否的固定事实问题"}` | 真实 LLM DM 裁定 |
| `action` | `{"action":"snake_case_semantic_action"}` | DM 确认后由引擎显式提交 |

每个 `Exit.trigger_id` 必须引用当前拍的触发器，`next_beat_id` 必须存在。跨拍
`transition_beat` 只能到当前拍 action 出口允许的目标。

### 5. KeyInfo、自动搜获与效果所有权

`KeyInfo` 是线索正文与持久化发现效果的唯一组合：

```json
{
  "id": "clue_id",
  "text": "玩家真正发现后可知的正文",
  "discovery_effects": {
    "flags_set": {"declared_flag": true},
    "grant_items": [
      {"item_id": "item_id", "quantity": 1, "recipient": "active_actor"}
    ]
  }
}
```

- 调查环境、阅读铭文、打开容器、发现暗格、审问或鉴定，必须等玩家实际完成相应行动后发现。
- 敌人随身携带且胜利后无需额外选择或检定即可取得的密信、钥匙等，仍由同拍唯一 `KeyInfo`
  保存正文和效果，再由 `Encounter.on_win_discoveries` 引用其 ID。
- 失败、撤退或战斗未结束时不能触发 `on_win_discoveries`。
- 同一个持久化 flag 只能由一个普通世界入口、一个 discovery 或一个 encounter 管理。
- 同一个 `item_id` 只能由一个 `KeyInfo.discovery_effects.grant_items` 发放。
- `loot_table` 不写角色背包，不得与 `grant_items` 重复承担关键物品职责。

### 6. Encounter

`Encounter` 可以包含 `id`、`monster_ids`、`surprised`、`random_seed`、`xp_reward`、
`on_win_flags`、`on_win_discoveries` 和 `loot_table`。其中：

- `monster_ids` 至少一项，并引用本拍在场且拥有固定卡面的 actor。
- `on_win_flags` 只写“战斗胜利本身立即成立”的已声明 flag。
- `on_win_discoveries` 只能引用当前拍存在的敌人随身线索，且不得重复。
- `loot_table` 是给结算叙述使用的摘要，不是第二套物品系统。

### 7. ActionDefinition

物品或任务特性只要产生机械效果，就必须定义 `ActionDefinition`：

- `source_kind`：`item` 或 `quest_feature`；`source_ref` 指向来源。
- `scopes`：`combat`、`world` 或二者。
- `requirements`：限定 flag、拍、地点或遭遇。
- `targeting`：限定阵营、生命状态、距离、固定 actor 和目标数量。
- `usage`：`unlimited`、`consume_item`、`once_per_combat` 或 `once_per_session`。
- `contract.check_templates`：固定攻击、豁免或属性检定。
- `contract.effect_templates`：只使用引擎支持的封闭效果和固定参数。

战斗效果包括伤害、治疗、临时 HP、状态、AC/攻击修正、区域移动和复苏；世界效果包括 flag、
物品、发现线索、地点移动与剧情拍迁移。不得生成自由文本脚本或只写描述、不写规则的效果。

## 五、提交前必须覆盖的校验

Prompt 的自检清单和后端校验至少覆盖：

1. `start_beat_id` 存在，时长、人数和公开元数据合法。
2. 至少有一个 `win` 结局和一个 `lose` 结局。
3. 所有非结局拍有出口，所有拍从起始拍或全局结局路径可达。
4. beat、location、actor、trigger、encounter、clue、flag、item、action 引用闭合。
5. 关键 NPC 有死亡续接，所有遭遇 actor 有固定战斗卡。
6. 全局胜利条件绑定正确 Boss 遭遇，不会因击败无关 NPC 成立。
7. 线索路线提供优势；若有真实硬门槛，存在一致的世界依据、反馈与补救路线。
8. `on_win_discoveries` 仅含同拍、不重复的敌人随身线索。
9. 每个持久化 flag 与 `item_id` 只有一个原子 owner。
10. `loot_table` 不承担持久化入库，所有规则行动使用引擎支持的封闭效果。
11. 最终内容是单一、可直接解析的 JSON 对象。

## 六、JSON 实时修改时的同步规则

### 只修改现有字段的故事内容

例如改 NPC 文案、地点描述、线索正文、数值、拍内连接或现有 ActionDefinition 参数，只需：

1. 修改对应 Canon JSON。
2. 运行 Canon 加载与校验测试。
3. 确认没有破坏引用闭合、可达性和唯一原子入口。

生成器在每次 Canon 编译前重新读取两份参考文件，因此不需要把相同内容再复制进 Python Prompt。
已经缓存的是聊天模型实例，不是参考 Canon 内容。

### 新增或改变字段、枚举、效果语义

这属于引擎契约变更，必须按以下顺序同步：

1. 修改 `src/model/canon.py` 或 `src/model/rule_action.py` 的数据形状与 `from_dict`。
2. 修改确定性校验器；若有运行时效果，再修改相应规则执行器。
3. 修改 `CANON_AUTHORING_RULE` 和必要的修复 Prompt。
4. 至少在一份参考 Canon 中给出合法实例；若两种叙事模式都受影响，两份都更新。
5. 更新本文的字段清单、双参考差异与自检清单。
6. 更新 Prompt、Canon、API/前端协议和运行时回归测试。

不能仅在 JSON 中加入新字段并期望模型或引擎自动理解；`Canon.from_dict` 未读取的字段会失去语义，
执行器未实现的效果也不能靠 Prompt 描述补齐。

### 变更影响矩阵

| 变更 | 必须同步检查 |
|---|---|
| Canon 顶层或子结构字段 | model、from_dict、validator、Prompt、参考 Canon、本文、测试 |
| Trigger/枚举值 | model enum、判定函数、Prompt、参考 Canon、协议测试 |
| ActionDefinition 效果 | action schema/编译器/执行器、Prompt、参考 Canon、战斗或世界测试 |
| 线索与物品结算 | discovery effect、Session 状态/API、Prompt、Canon、幂等测试 |
| 公开故事元数据 | Canon、Pydantic schema、服务接口、前端类型与故事广场 |
| 参考 Canon 内容 | 两份 Canon 的加载/校验测试；字段不变时无需改 Prompt 源码 |
| 故事生成模型 | 三个 `STORY_*_MODEL` 职责、`.env.example`、模型注册表与路由测试 |

## 七、模型配置

故事模块从中央目录按职责选择模型；未提供职责覆盖时，访谈使用 Fast，编译与修复使用 Pro：

```dotenv
LLM_MODELS=deepseek/deepseek-v4-pro,deepseek/deepseek-v4-flash
LLM_REASONING_MODEL=deepseek/deepseek-v4-pro
LLM_FAST_MODEL=deepseek/deepseek-v4-flash

# 可选职责覆盖
STORY_INTERVIEW_MODEL=deepseek/deepseek-v4-flash
STORY_AUTHORING_MODEL=deepseek/deepseek-v4-pro
STORY_REPAIR_MODEL=deepseek/deepseek-v4-pro
```

职责变量必须引用 `LLM_MODELS` 中登记的 `供应商/模型 ID` 复合名。缺少目录配置、引用未知模型
或模型调用失败时显式报错，不回落到假故事。修改环境变量后需要重启服务。

## 八、推荐回归命令

```powershell
uv run python -m pytest test/test_canon_authoring_prompt.py test/test_story_api.py
uv run python -m pytest test/test_story_continuity.py
uv run python -m py_compile src/story/generator.py src/story/prompt.py
```

若修改了规则行动、战斗结算或 Session 投影，还应补跑对应的 action、combat 与 session 测试。
