# 02 · LangGraph 流程设计

> 把 [`../image.png`](../image.png) 那张流程图落成 `StateGraph`。每个节点读写 [`01-战斗状态定义.md`](./01-战斗状态定义.md) 的 `CombatState`。
> 中断细节见 [`03-中断交互协议.md`](./03-中断交互协议.md)。

---

## 1. 图结构（节点 ↔ image.png 对照）

| 图节点 | image.png 框 | 颜色/角色 | 职责 |
|---|---|---|---|
| `enter_combat` | 战斗触发，进入战斗模式 | 引擎 | 初始化 `CombatState`，加载参战者，摆好区域 |
| `judge_surprise` | DM 判定是否突袭 | DM | 标记 `被突袭=True` 的一方（其首回合被跳过） |
| `roll_initiative` | 引擎掷先攻 → 排定行动顺序 | 引擎 + 玩家中断 | 玩家报 d20、怪物引擎掷，写 `先攻顺序` |
| `next_turn` | 轮到下一位参战者 | 引擎 | 推进 `当前指针`，处理跳过 + 回合开始结算 |
| `declare_action` | 声明行动与目标 | 玩家中断 / DM | 玩家选攻击·技能·道具·创意；怪物由 DM 决定 |
| `resolve_action` | 引擎掷骰，比对 AC/DC | 引擎 + 玩家中断 | 玩家报攻击/伤害骰，引擎判命中、扣 HP、加状态 |
| `narrate` | DM 叙述本回合结果 | DM | 把 `本回合事件` 讲成故事，推流给前端 |
| `check_end`（条件边） | 一方全部倒下？ | 引擎 | 否→`next_turn`；是→`settle` |
| `settle` | 结算并回到剧情 | 引擎 | 写回 HP / 战利品 / flag，结束子图 |

### 边

```
START → enter_combat → judge_surprise → roll_initiative → next_turn
next_turn → declare_action → resolve_action → narrate → check_end
check_end ─(战斗结果=="进行中")→ next_turn      # image 里"否，回到上一位的下一位"
check_end ─(否则)──────────────→ settle → END
```

`check_end` 用 LangGraph 的条件边（`add_conditional_edges`），返回下一个节点名。

---

## 2. 各节点行为细则

### 2.1 `enter_combat`（引擎）
- 从 `场景上下文` + 角色卡/怪物卡构造 `combatants`，给每个加运行时字段（`阵营 / 是否玩家控制 / 操控者`）。
- 置 `阶段="初始化"`、`战斗结果="进行中"`、`当前轮次=0`。
- 不需要玩家输入。

### 2.2 `judge_surprise`（DM）
- 调 LLM，依据 `场景上下文`（谁在潜行、谁没察觉）判定哪一方被突袭，给对应 combatant 置 `被突袭=True`。
- **v0 简化**：纯叙事判定，不掷隐匿/察觉。日后要做对抗时，再在此节点加"潜行方玩家掷敏捷(隐匿) vs 对方被动察觉"的中断。
- 双方都不潜行 → 无人被突袭，直接过。

### 2.3 `roll_initiative`（引擎 + 玩家中断）
- 遍历 `combatants`：
  - `是否玩家控制==True` → **`interrupt`** 请玩家报 `d20`（类型 `掷先攻`），引擎加 `先攻调整值` 得 `先攻值`。
  - 怪物 → 引擎掷 `d20 + 先攻调整值`。
- 按 `先攻值` 降序排序写入 `先攻顺序`；平手用 `原始数据.md` 规则（玩家间自定 / 含 DM 角色由 DM 定，v0 可简单按敏捷调整值再随机）。
- 置 `当前指针=0`、`当前轮次=1`、`阶段="掷先攻"`。

> 收集多个玩家的先攻：按顺序逐个 `interrupt`，或一次性发"全体掷先攻"的批量中断（见 03 文档"批量中断"）。

### 2.4 `next_turn`（引擎）
- 取 `行动者 = 先攻顺序[当前指针]`。
- **回合开始结算**：处理 `持续伤害` 等状态（扣 HP，可能直接倒下）、递减状态 `剩余回合`。
- **跳过判断**：若行动者 `存活状态=="倒下"`，或 `被突袭 and 当前轮次==1`，或带 `眩晕` → 不行动，直接推进指针（必要时进位到下一轮），重新选行动者。
- 置 `阶段="回合中"`，清空 `当前行动`、`本回合事件`。
- 指针推进规则：`当前指针 += 1`；若 `>= len(先攻顺序)` 则 `当前指针=0`、`当前轮次 += 1`。
  - **注意**：推进在 `check_end` 之后进行，保证图里只有一条回边。具体把"推进"放在 `next_turn` 入口还是 `check_end` 出口，实现时二选一并保持一致即可。

### 2.5 `declare_action`（玩家中断 / DM）
- `是否玩家控制==True` → **`interrupt`**（类型 `声明行动`）：把可选项推给玩家——
  - 攻击：列出 `攻击` 里各项 + 射程内的合法目标（按 `当前区域` 过滤）。
  - 技能：列出 `已学技能` 中有充能的项。
  - 道具：列出 `背包` 可用项。
  - 创意：自由文本，交 DM 临场裁定要不要检定。
  - 移动：可选，改 `当前区域`（本版"区域"粒度，不算格子）。
- 怪物 → 调 LLM 决策：依战场态势选目标与动作（只决策，不掷骰）。
- 产出写入 `当前行动`，例：`{"行动类型":"攻击","攻击名":"长剑","目标id":"mob_goblin_1"}`。

### 2.6 `resolve_action`（引擎 + 玩家中断）
按 `当前行动` 类型走确定性结算：
- **攻击**：
  1. 取攻击的 `命中加值` 与目标 `AC`。
  2. 行动者是玩家 → **`interrupt`** 请其报攻击 `d20`（类型 `攻击检定`）；是怪物 → 引擎掷。
  3. 判定：原始 20 必中且重击；原始 1 必失；否则 `d20+命中加值 ≥ AC` 命中。
  4. 命中 → 取 `伤害骰`；玩家 → **`interrupt`** 报伤害（类型 `伤害掷骰`，重击则翻倍骰数）；怪物 → 引擎掷。扣目标 `当前HP`，必要时往目标 `状态` 加一条。
  5. 目标 `当前HP<=0` → 置 `存活状态="倒下"`。
- **技能/道具**：按其效果积木结算（命中/豁免/治疗/加状态），扣 `当前充能` 或 `数量`；需要豁免时让**被作用方**玩家掷豁免（中断）。
- **创意**：DM 给出 DC，行动者掷对应属性检定（玩家中断 / 怪物引擎掷），引擎比对 DC。
- 每一步都往 `本回合事件` 追加结构化记录，并同步进 `战斗日志`。

> **减少往返的建议**：玩家攻击可"一次中断收齐"——同一个中断里让玩家同时报攻击骰和伤害骰，引擎命中才用伤害、未命中则忽略。详见 03 文档。

### 2.7 `narrate`（DM）
- 读 `本回合事件`，调 LLM 生成叙述，通过现有 `astream_agent_collect` 把 token 推流到前端（见现项目 `writer.py`）。
- 纯表演，不改 `combatants` 数值。

### 2.8 `check_end`（引擎条件边）
- 若 `敌人` 阵营全员 `倒下` → 置 `战斗结果="玩家胜"` → 去 `settle`。
- 若 `玩家` 阵营全员 `倒下` → 置 `战斗结果="玩家败"` → 去 `settle`。
- 否则 `战斗结果` 保持 `进行中` → 回 `next_turn`。

### 2.9 `settle`（引擎）
- 置 `阶段="结束"`。
- 写回世界库：各玩家 `当前HP / 状态 / 背包`；按 `场景上下文` 的战利品表发奖；置剧情 `flag`（如 `战斗_哥布林营地=已清剿`）。
- 子图返回最终 `CombatState` 给上层（日后的 DM 主图据此续写剧情）。

---

## 3. 图骨架（伪代码·仅示意）

```python
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver  # 换掉 MemorySaver，持久化整场战斗

def build_combat_graph():
    g = StateGraph(CombatState)

    g.add_node("enter_combat", enter_combat)
    g.add_node("judge_surprise", judge_surprise)
    g.add_node("roll_initiative", roll_initiative)
    g.add_node("next_turn", next_turn)
    g.add_node("declare_action", declare_action)
    g.add_node("resolve_action", resolve_action)
    g.add_node("narrate", narrate)
    g.add_node("settle", settle)

    g.add_edge(START, "enter_combat")
    g.add_edge("enter_combat", "judge_surprise")
    g.add_edge("judge_surprise", "roll_initiative")
    g.add_edge("roll_initiative", "next_turn")
    g.add_edge("next_turn", "declare_action")
    g.add_edge("declare_action", "resolve_action")
    g.add_edge("resolve_action", "narrate")

    g.add_conditional_edges("narrate", check_end, {
        "continue": "next_turn",   # 战斗结果 == 进行中
        "end": "settle",
    })
    g.add_edge("settle", END)

    return g.compile(checkpointer=SqliteSaver(...))  # thread_id = f"combat:{房间id}"
```

> `check_end` 作为条件函数（不是节点），读 `战斗结果` 返回 `"continue"` / `"end"`。
> 也可保留独立 `check_end` 节点专门改写 `战斗结果`，再让条件函数只做读路由——二选一，保持一处。

---

## 4. 多人 / 多怪要点

- **顺序由先攻定**，引擎严格按 `先攻顺序` 串行点名，天然支持 1–8 人；同一时刻只有一个中断在等待，不存在并发输入竞争。
- **怪物可成组**：`原始数据.md` 允许相同怪物共用一个先攻；实现时给同组怪物同一 `先攻值` 即可。
- **每场战斗一个 `thread_id`**（如 `combat:room_42`），换人发言、断线重连、服务重启都能从 checkpointer 恢复到正等待的那个中断。
