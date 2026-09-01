# StoryPlan 细粒度渐进生成方案

> 文档状态：已实现  
> 实现基线：2026-08-12  
> 适用文件：`src/schemas/story.py`、`src/story/generator.py`、`src/story/prompt.py`、`src/services/story_service.py`

## 1. 范围

本期只拆分 StoryPlan 生成。Canon 分片、公开 API、前端类型、SQLite 表结构和最终 Canon wire format 保持不变。

生产路径不再要求模型一次返回完整 StoryPlan。生成器以 `StoryPlanWorkState` 累积已经通过校验的小产物；每轮只发送确认稿、当前目标、当前严格 schema 和完整的精简状态视图，不携带历史 Prompt，也不让模型生成摘要。

## 2. 实际阶段与字段所有权

| 阶段 | 模型填写 | 代码锁定或派生 | 单批上限 |
| --- | --- | --- | ---: |
| 章节骨架 | campaign ID；Act 的 id、purpose、turning_point、playable_beat_count | Act 数与可玩 Beat 总数校验 | Act 5 |
| Beat 大纲 | id、kind、estimated_minutes、objective | act_id；首拍 opening；末拍 climax | 3 |
| 分支蓝图 | source、两个 choice、reconverge、两条不同后果 | 出口图 | 3 |
| Beat 细节 | pressure、dramatic_question、entry_hook、fail_forward | 保留大纲字段 | 3 |
| 数量清单 | actor、flag、item、action、payoff 数量 | location、encounter、clue 数量 | 1 对象 |
| 实体清单 | id、name、summary | 分批与全局 ID 注册 | 5 |
| Beat 放置 | location、actor、clue、encounter 引用 | 引用校验与唯一 owner | 3 |
| 出口文案 | condition_summary、consequence | 目标 ID 和顺序 | 每拍最多 2 |
| 线索 | answers、unlocks、alternative_approaches | acquisition_owner | 3 |
| 伏笔 | flag、setup Beat、payoff Beat、description | Beat.payoff_flag_ids | 3 |
| 双结局 | objective、required_facts、payoffs | 固定 ID、outcome、Beat 结构 | 2 |
| 效果 owner | effect_id、owner_kind、owner_id | effect_kind | 3 |

所有模型输出类型均为 `extra="forbid"`。复杂对象使用最多 3 项的 `PlanComplexBatch[T]`，简单实体使用最多 5 项的 `PlanSimpleBatch[T]`；二者基于通用 `PlanBatch[T]`。

数量边界以可玩 Beat 数 `P` 为准：

- actor：`1..2P`；
- flag、item、action：`0..P`；
- payoff：`0..flag_count`；
- location、encounter、clue：精确采用确认稿 `scale_profile`。

## 3. 固定路线骨架

StoryPlan 只接受稳定的二选一汇流结构：

```text
普通 Beat -> 下一顺序 Beat
source -> choice_a | choice_b -> reconverge
climax -> ending_win
全局失败条件 -> ending_lose
```

每个分支占顺序列表中的连续四拍。分支窗口不能重叠；唯一允许的共享是前一窗口的 reconverge 同时作为下一窗口的 source。模型选择合法窗口并编写后果，代码根据窗口生成出口目标。最终验收再次比较固定拓扑，修复模型不能改变出口目标。

若分支数为 `B`，任务提交边界要求：

```text
playable_beats >= 3B + 2
```

不满足时返回 HTTP 422，不创建任务。

## 4. Artifact 与恢复

沿用 `generation_artifacts` 表，不新增字段或迁移。确定性 key 为：

```text
plan:frame
plan:beat_outline:<act_id>
plan:branches
plan:beat_detail:<batch>
plan:entity_budget
plan:entities:<category>:<batch>
plan:placement:<batch>
plan:routes:<beat_id>
plan:clues:<batch>
plan:payoffs:<batch>
plan:endings
plan:owners:<batch>
plan
```

只有通过 Pydantic、target、数量、引用、全局 ID 和阶段契约校验的结果才保存。恢复时按同一顺序重新解析和校验现有 artifact，仅调用缺失阶段。已有完整 `plan` 的旧任务直接进入原 Canon 分片流程；没有完整 `plan` 的任务从新渐进流程开始。

`plan:frame` 落库时立即通过现有 reservation 表原子预留 campaign ID。任务恢复优先从 `plan`，其次从 `plan:frame` 恢复该预留。失败、取消和过期仍沿用现有释放逻辑。

规划阶段公开进度保持在 10–24%，只显示中文阶段名；完整 `plan` 完成后，现有 Canon 分片从 25% 开始。

## 5. 修复与最终验收

每个缺失小阶段执行一次初稿调用。字段或阶段契约不合法时，只允许一次同阶段定向修复；第二次仍不合法立即失败。无法解析的 JSON 也消耗这一次修复机会。模型调用或配置错误继续显式失败，不使用离线或模板兜底。

小阶段全部完成后，代码编译现有 `StoryPlanCandidate`，调用既有归一化、Pydantic 和完整确定性校验：

- 结构问题：最多一次完整重规划；
- 局部问题：最多一次允许区段修复；
- 修复后再次执行完整校验与固定拓扑校验；
- campaign ID 必须保持为 `plan:frame` 已预留值；
- 未完全通过时不保存最终 `plan`，也不创建公开草稿。

小阶段各自记录实际修复次数。最终 `plan` artifact 的 attempt 固定为 0，避免再次累计前面已经记录的修复。

## 6. 验收覆盖

离线测试覆盖：封闭 schema、3/5 项批次上限、target ID、全局 ID 去重、三种长度数量边界、零/一/二分支 DAG、提交 422、完整状态 Prompt、逐 artifact 恢复、旧完整 plan 兼容、小阶段一次修复、最终一次修复，以及非法结果不落最终 plan。

例行验证命令：

```powershell
uv run python -m unittest discover -s test -p "test_*.py"
```

## 7. 后续唯一维护清单

后续细粒度生成工作只在本节维护：

1. 访谈阶段改为设计稿增量字段更新，避免每轮返回完整 `StoryDesignBrief`。
2. `top_level` 改为代码骨架加小型创作载荷。
3. Cast、Location 改为逐实体生成和条目级修复。
4. Act Canon 改为逐 Beat 生成任务卡、线索、Encounter 和 Trigger 载荷。
5. ActionDefinition、Ending 和连贯性修复改为最小对象单元。
6. 增加真实 token、耗时、首次通过率和阶段修复率统计。
7. 根据真实指标增加上下文裁剪、按职责输出预算和有限并发。
8. 迁移完成后删除未被公开路由使用的一次性 `generate_canon()` 路径。

本期不支持三路分支或任意自由 DAG，不增加依赖、数据库迁移、模板故事或离线生成兜底。真实 short/standard/long 模型基准留作人工评估。
