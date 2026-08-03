# Canon 分片确定性校验与局部修复方案

> 文档状态：计划实施
> 设计基线：2026-08-03
> 单一问题：模型被要求重复生成大量确定性字段，校验失败后又重写完整分片，成功率和稳定性不足
> 目标文件：`src/story/generator.py`、`src/story/validation.py`、`src/story/prompt.py`、`src/model/canon.py`

## 1. 目标

建立一条固定的“代码编译 + AI 创作 + 代码校验 + AI 局部修复”流水线：

```text
StoryPlan
  -> 代码生成不可变骨架
  -> AI 只返回创作载荷
  -> 代码白名单合并
  -> 条目级确定性校验
  -> 按最小修复单元调用 AI
  -> 重新合并和校验
  -> 分片完整校验
  -> 持久化
```

本方案不降低现有运行时规则校验，也不允许代码创作故事内容。

## 2. 代码和 AI 的职责边界

### 2.1 代码负责

所有能够从确认稿、StoryPlan、ID registry 或固定协议唯一推导的字段：

| 对象 | 代码拥有的字段 |
| --- | --- |
| top level | `campaign_id`、时长、档位、Act 数、推荐人数、`runtime_location_scoping`、`declared_flags`、`start_beat_id` |
| Beat | `id`、`act_id`、`kind`、`estimated_minutes`、`objective`、`pressure`、`location_ids`、`relevant_clue_ids`、`payoff_flag_ids` |
| Exit | 数量、`trigger_id`、`next_beat_id`，以及 Plan 中已有的 consequence |
| Trigger 槽位 | ID 和与出口的一一对应关系 |
| KeyInfo 槽位 | clue ID 集合 |
| Encounter 槽位 | 是否存在和 encounter ID |
| Actor 槽位 | actor ID 集合；名字和固定卡面可从 Cast 引用 |
| Ending | Beat ID、Act、时长、地点、空 exits、空 advance conditions、win/lose 路由 |

代码对这些字段执行覆盖，而不是让模型生成后再比较是否逐字一致。

### 2.2 AI 负责

- 标题、场景描述、气氛和叙事内容；
- Trigger 的 `kind`、`predicate`、`description`；
- KeyInfo 的内容、`location_id`、`discovery_hints` 和被授权效果；
- Actor 的 disposition、当前地点和运行时类型；
- Encounter 的敌人编排、奖励、战斗叙事参数；
- ActionDefinition 的合法规则表达；
- 角色动机、线索答案、伏笔回应和结局文案。

这些字段无法由 Plan 唯一确定，代码只能验证，不能猜测或用模板补齐。

## 3. 创作载荷 Schema

长期方案是不再要求模型输出完整 Canon Beat，而是输出按稳定 ID 索引的创作载荷。例如：

```json
{
  "beats": [
    {
      "id": "beat_archive",
      "title": "封闭前的档案馆",
      "entry_state": {
        "location_id": "location_archive",
        "description": "...",
        "actors": [
          {
            "actor_id": "actor_scholar",
            "disposition": "friendly",
            "location_id": "location_archive",
            "type": "monster"
          }
        ]
      },
      "key_info_by_id": {
        "clue_eclipse_map": {
          "content": "...",
          "location_id": "location_archive",
          "discovery_hints": ["...", "..."]
        }
      },
      "triggers_by_id": {
        "trigger_beat_archive_1": {
          "kind": "action",
          "predicate": {},
          "description": "..."
        }
      },
      "encounter_payload": null,
      "stuck_fallback": {}
    }
  ]
}
```

`compile_act_fragment(plan, payload, cast)` 负责组装完整 Beat。`next_beat_id` 等结构字段根本不进入模型输出 schema，因此不会再出现“模型写错后反复修”的问题。

## 4. 渐进落地方式

如果一次改造 Prompt 和 schema 风险过高，可以分两步实施。

### 4.1 第一阶段：完整分片后归一化

保留现有模型输出格式，增加：

```python
def apply_plan_derived_fields(
    fragment_kind: str,
    fragment: dict[str, Any],
    plan: StoryPlan,
) -> NormalizationResult:
    ...
```

要求：

- 使用 `deepcopy`，不在校验函数内隐式修改入参；
- 只覆盖上表中的代码字段；
- 覆盖前记录字段差异的路径和数量；
- exits 和 triggers 数量不一致时不得使用 `zip` 静默截断，而是留下结构错误；
- 不自动创建缺失的 KeyInfo、Encounter 或 Trigger 语义；
- 初次生成、修复响应、连贯性修复响应都必须经过同一入口。

### 4.2 第二阶段：切换为创作载荷

按分片逐步切换：

1. endings；
2. top level；
3. 单个 Act Beat；
4. cast、locations；
5. actions。

每切换一种分片，都保留组装后完整 Canon 的原有校验，确认运行时 wire format 不变。

## 5. 结构化校验问题

把字符串错误升级为统一对象：

```python
class FragmentValidationIssue(BaseModel):
    code: str
    fragment_kind: str
    object_kind: Literal[
        "top_level", "cast", "location", "beat", "clue", "trigger", "encounter", "action"
    ]
    object_id: str | None
    path: tuple[str | int, ...]
    repair_mode: Literal["code", "ai_item", "ai_fragment", "replan"]
    message: str
    context: dict[str, Any] = Field(default_factory=dict)
```

`message` 用于日志和测试，不再承担错误路由。修复路由只依赖稳定的 `code`、`object_kind` 和 `object_id`。

## 6. JSON 拆分修复层级

按以下顺序选择最小修复单元：

```text
字段组
  -> 单个数组条目
  -> 单个 Beat
  -> 单个 Act 分片
  -> StoryPlan 结构重规划
```

### 6.1 字段组修复

适用于对象 ID 正确、只有一个语义子结构非法的情况：

- 一个 Trigger 的 `kind + predicate + description`；
- 一个 KeyInfo 的内容、地点、提示和 discovery effects；
- 一个 Encounter 的地点、monster IDs 和胜利结算；
- 一个 CombatCard；
- 一个 ActionDefinition contract。

模型返回固定 envelope：

```json
{
  "repair_kind": "trigger_payload",
  "object_id": "trigger_beat_archive_1",
  "payload": {}
}
```

代码只允许替换该字段组，不能让模型同时修改 Beat ID 或出口。

### 6.2 单条目修复

适用于 Cast、Location、ActionDefinition 或 KeyInfo 某一项完整非法。Prompt 只携带：

- 当前条目；
- 该条目的紧凑 schema；
- 与该条目直接相关的 Plan 摘要和合法 ID；
- 结构化错误列表。

模型返回一个完整条目，代码按 ID 替换。

### 6.3 单 Beat 修复

多个语义字段互相依赖时，修复完整创作载荷 Beat，但仍不让模型返回代码拥有的字段。适用于：

- Actor、Encounter 和 combat Trigger 互相不一致；
- KeyInfo、stuck fallback 和推进条件互相不一致；
- 一个 Beat 内同时存在多个运行时错误。

### 6.4 Act 分片修复

仅在跨 Beat 的语义衔接无法通过单 Beat 修复时使用，例如本 Act 内线索在错误 Beat 被揭示。返回本 Act 的全部创作载荷，代码重新编译整个 Act。

### 6.5 不允许局部修复的情况

以下问题必须退回 StoryPlan：

- 缺少全局实体 ID；
- Beat 数量或 Act 归属需要改变；
- 出口目标或 DAG 拓扑需要改变；
- 分支汇流点需要改变；
- owner 类型或 owner 对象需要在不同类别间调整。

## 7. 固定修复算法

```python
candidate, normalization = compile_or_normalize_fragment(
    plan,
    ai_payload,
    compiled_dependencies,
)
issues = validate_fragment(candidate)

while issues and repair_budget > 0:
    groups = group_issues_by_repair_unit(issues)
    for group in groups:
        repaired_payload = await repair_one_unit(group)
        candidate = merge_whitelisted_payload(candidate, repaired_payload, group)
    candidate, normalization = compile_or_normalize_fragment(
        plan,
        candidate,
        compiled_dependencies,
    )
    issues = validate_fragment(candidate)

if issues:
    raise StoryGenerationError(...)
persist_validated_fragment(candidate)
```

同一轮中互不相关的修复单元可以限并发调用；有依赖的修复单元必须按依赖顺序处理。每轮结束后统一重新校验整个分片，防止局部修复破坏其它对象。

## 8. 依赖上下文裁剪

局部修复只传直接依赖：

| 修复单元 | 必需上下文 |
| --- | --- |
| Trigger | 当前 Beat 出口、合法 flags/items/locations/encounter ID |
| KeyInfo | 当前 clue 概要、地点、owner ledger 中对应效果 |
| Encounter | 当前 Beat actors、Cast 卡面、encounter ID |
| Actor entry | 当前 Beat 地点、对应 Cast 条目 |
| Action | requirements 涉及的 Beat/location/encounter、owner ledger |

不得在每次条目修复时重复发送完整 StoryPlan、全部参考 Canon 和其它 Act。

## 9. 持久化与恢复

- SQLite 只保存完整通过校验的分片，不保存半修复数组作为 validated artifact。
- 可以额外保存非公开的修复检查点，但必须带 `validated=false` 和 TTL。
- 恢复任务时，旧版完整分片先执行归一化和校验；通过后可以继续使用。
- 创作载荷 schema 引入版本号，例如 `fragment_payload_version=1`。
- 连贯性修复也必须返回创作载荷并走相同编译器，不能绕过代码字段所有权。

## 10. 测试要求

1. 模型写错 `act_id/kind/minutes/location_ids` 时由代码纠正且不调用 AI 修复。
2. exits 数量不一致时不会被 `zip` 静默吞掉。
3. 模型无法修改 `next_beat_id` 和 Trigger ID。
4. 非法 Trigger 只修复该 Trigger 字段组。
5. 非法 CombatCard 只修复对应 Cast 条目。
6. Actor、Encounter、combat Trigger 联动错误升级为单 Beat 修复。
7. 新建全局 ID 的请求被拒绝并升级到 replan。
8. AI 返回白名单外字段时不会进入最终 Canon。
9. 局部修复后重跑完整分片和完整 Canon 校验。
10. 连贯性修复不能绕过归一化入口。

## 11. 验收标准

- 代码拥有的字段不再出现在 AI 修复错误中。
- 单个条目错误不会触发整个 Act 重写。
- 分片平均修复调用次数不高于 1。
- 局部修复不能改变 StoryPlan 的 ID、拓扑或 owner。
- 最终 Canon wire format 与当前运行时保持兼容。
