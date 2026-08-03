# StoryPlan 结构收敛方案

> 文档状态：计划实施
> 设计基线：2026-08-03
> 单一问题：StoryPlan 同时包含多份重复事实，修复 Prompt 又禁止完成数量和拓扑修复，导致修复不收敛
> 目标文件：`src/schemas/story.py`、`src/story/generator.py`、`src/story/validation.py`、`src/story/prompt.py`

## 1. 问题定义

当前 StoryPlan 同时要求模型维护多组等价信息：

- `StoryPlan.scale_profile` 与已确认 `StoryDesignBrief.scale_profile`；
- `PlanAct.beat_ids` 与 `PlanBeat.act_id`；
- `PlanAct.estimated_minutes` 与所属 Beat 时长之和；
- `PlanBranchPoint.choices` 与源 Beat 的 `exits[].to_beat_id`；
- `PlanBeat.payoff_flag_ids` 与 `foreshadowing_payoffs[].payoff_beat_id`；
- 实体数组与代码派生的 ID registry。

这些值只要有一处偏差，就会触发全量 StoryPlan 修复。更严重的是，当前修复契约要求“不得新增、删除或重命名已有 ID”，但“Beat 数量不符合确认目标”必须新增或删除 Beat 才能解决。这类错误在现有契约下不可收敛。

本方案只解决 StoryPlan 的稳定收敛，不处理 Canon 分片、网络重试或并发。

## 2. 设计原则

### 2.1 每项事实只保留一个权威来源

| 事实 | 权威来源 | 由代码派生的字段 |
| --- | --- | --- |
| 故事规模 | `StoryDesignBrief.scale_profile` | `StoryPlan.scale_profile` |
| Beat 所属 Act | `PlanBeat.act_id` | `PlanAct.beat_ids` |
| Act 时长 | `PlanBeat.estimated_minutes` | `PlanAct.estimated_minutes` |
| 分支出口 | `PlanBeat.exits` | `PlanBranchPoint.choices` |
| 伏笔回收 Beat | `foreshadowing_payoffs` | `PlanBeat.payoff_flag_ids` |
| 全局 ID 集合 | StoryPlan 实体和 Beat | `story_plan_id_registry()` |

第一阶段可以保留现有 Pydantic 字段以减少迁移范围，但模型输出后必须由代码覆盖派生字段。第二阶段再考虑从“模型输出 schema”中移除这些字段，由内部编译器构造完整 `StoryPlan`。

### 2.2 结构错误和局部错误使用不同修复模式

不得再用同一条“保留全部 ID”Prompt 处理所有错误。

```text
局部错误
  -> 保持 ID 和拓扑
  -> 只修复允许区段

结构错误
  -> 允许调整 ID、Beat 数量和图结构
  -> 重规划完整依赖闭包
```

## 3. 固定处理流程

```text
LLM 生成 StoryPlanCandidate
  -> 宽松候选结构解析
  -> normalize_story_plan_candidate()
  -> StoryPlan.model_validate()
  -> validate_story_plan()
  -> classify_story_plan_issues()
      -> 无错误：持久化
      -> 仅局部错误：sectional repair，最多 2 次
      -> 含结构错误：structural replan，最多 1 次
  -> 每次候选重新归一化和完整校验
  -> 错误集合重复或预算耗尽：显式失败
```

### 3.1 代码归一化

建议新增只负责模型输出形状的 `StoryPlanCandidate`。它允许代码派生字段缺失，但仍严格限制对象类型、额外字段和 ID 基本格式。候选对象不能进入业务流程或持久化为有效 Plan。

随后新增纯函数：

```python
def normalize_story_plan_candidate(
    raw: dict[str, Any],
    brief: StoryDesignBrief,
) -> dict[str, Any]:
    """只覆盖能够唯一推导的 StoryPlan 字段，不创作剧情。"""
```

处理顺序固定为：

1. `scale_profile` 覆盖为确认稿值，包括补齐模型遗漏的该字段。
2. 按 `beats[].act_id` 重建每个 Act 的 `beat_ids`。
3. 按所属 Beat 重算每个 Act 的 `estimated_minutes`。
4. 对已经存在的 `branch_points`，按源 Beat exits 重建 `choices`。
5. 按 `foreshadowing_payoffs` 重建所有 Beat 的 `payoff_flag_ids`。
6. 不改 Beat 数量、出口拓扑、实体 ID、owner 选择或剧情文本。

归一化完成后再调用最终 `StoryPlan.model_validate()`。这样既允许模型不再重复派生字段，又不会降低最终 StoryPlan 的严格 schema。

归一化应返回变更摘要，供指标记录，但不得把完整故事内容写入日志。

### 3.2 错误分类

建议让校验器返回结构化问题，而不只返回中文字符串：

```python
class PlanValidationIssue(BaseModel):
    code: str
    path: tuple[str | int, ...]
    category: Literal["local", "structural"]
    affected_sections: set[str]
    message: str
```

应归为 `structural` 的错误至少包括：

- Beat、Act、Location、Encounter、Clue 数量不符合确认目标；
- 重复 ID 或跨类别 ID 冲突；
- DAG 有环；
- Beat 不可达或不能通往结局；
- 分支数量、分支源或汇流拓扑错误；
- 删除或新增对象后会影响 owner、payoff、clue graph 的错误。

其余不改变对象集合和拓扑即可完成的错误归为 `local`。

### 3.3 局部区段修复

局部修复返回完整区段而不是自由 JSON Patch：

```json
{
  "repair_kind": "story_plan_sections",
  "sections": {
    "foreshadowing_payoffs": [],
    "effect_owner_ledger": []
  }
}
```

代码根据 `affected_sections` 建立白名单，只合并被授权区段。合并后再次执行归一化和完整校验。

不建议让模型输出 RFC 6902 JSON Patch。数组索引在新增、删除 Beat 后不稳定，错误路径也很难审计。以顶层区段为原子单元更适合当前 schema。

### 3.4 结构重规划

结构错误不能继续锁死全部 ID。建议新增 `build_story_plan_replan_prompt()`，明确：

- 保持确认稿、内容边界和已预留 `campaign_id_candidate`；
- 允许重建 `acts`、`beats` 和受影响实体 ID；
- 同时返回依赖闭包，不能只返回 `beats`；
- 返回前必须满足目标数量、DAG、分支、payoff 和 owner 契约。

依赖闭包至少包含：

```text
acts
beats
entities
clue_graph
branch_points
foreshadowing_payoffs
ending_routes
effect_owner_ledger
```

结构重规划完成后视为新的候选计划，不能与旧计划按 ID 做局部拼接。

## 4. 收敛控制

- 局部修复最多 2 次。
- 结构重规划最多 1 次。
- 对排序后的 `(code, path)` 计算错误指纹。
- 连续两轮指纹相同，立即停止，不继续消耗模型调用。
- 错误数量减少但类别升级为 structural 时，立即切换结构重规划。
- 只有校验完全通过的 StoryPlan 才允许持久化为 `plan` artifact。

## 5. 代码改造建议

新增：

```text
src/story/plan_normalizer.py
src/story/plan_repair.py
```

职责建议：

- `plan_normalizer.py`：确定性派生和变更摘要。
- `plan_repair.py`：问题分类、依赖闭包和白名单合并。
- `validation.py`：返回结构化 issue；在 API 边界再格式化中文错误。
- `generator.py`：只负责编排固定状态机。
- `prompt.py`：拆分 local repair 与 structural replan Prompt。

## 6. 测试要求

至少增加以下离线测试：

1. ScaleProfile 与确认稿不一致时由代码覆盖，不调用修复模型。
2. Act `beat_ids` 和时长错误时由代码重建。
3. branch choices 与 exits 不一致时由代码重建。
4. payoff 双重表示不一致时由代码重建。
5. Beat 数量错误被分类为 structural，Prompt 允许增删 ID。
6. 结构重规划合并时不会保留旧计划的悬空 branch、owner 或 payoff。
7. 连续两次错误指纹相同会提前终止。
8. 未通过完整校验的候选不会写入 SQLite artifact。

## 7. 验收标准

- 不再出现“要求修改数量，同时禁止增删 ID”的 Prompt。
- 纯派生字段错误不触发 AI 修复。
- StoryPlan 平均 AI 修复次数不高于 1。
- 结构错误最多经历一次明确的重规划，不进入 10 次循环。
- 现有 DAG、规模、owner、路径和节奏校验全部保留。
