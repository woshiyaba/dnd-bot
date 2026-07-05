# 普通 reply 流式化与图流工具抽取方案

## 背景

当前会话接口已经通过 WebSocket 旁路转发 LangGraph 的 custom 事件。检定结果、战斗叙述、切拍叙述等调用 `dm_narrate()` 的路径，可以在前端逐 token 展示。

但最常见的普通 `reply` 仍然不是 token 级流式：

- `src.dm.world_bridge.decide_turn()` 要求 LLM 输出 JSON。
- `intent=reply` 时，JSON 里直接包含完整 `say`。
- `src.session.dm_subgraph.narrate_reply()` 只把这个完整 `say` 整段推给前端。

所以普通对话的延迟体感仍然是：先等 LLM 完整生成并解析 JSON，再一次性显示文本。

另一个问题是 `src.session.engine.SessionEngine._astream_interpret()` 里消费 `graph.astream(..., stream_mode=["custom", "values"])` 的循环是通用形态，后续 combat/story/其他 graph facade 可能都会需要同类逻辑，适合抽到公共工具。

## 目标

1. 把普通 `reply` 的玩家可见叙述从 JSON 决策中拆出，改为独立叙述节点调用 `dm_narrate()`，获得 token 级流式输出。
2. 保留 DM 决策必须使用真实 LLM 的约束，不增加 mock、模板、启发式兜底。
3. 把 graph 流式消费逻辑抽成公共方法，供 session 之外的 graph facade 复用。
4. REST 响应仍返回最终 payload，WebSocket 只负责增量展示，不作为最终状态来源。

## 非目标

- 不改前端 WebSocket 协议形态，继续使用 `node_start`、`stream`、`node_end`、`session_update`。
- 不让 LLM 计算规则结果、HP、命中、伤害。
- 不把 WebSocket 依赖引入 `src.session`、`src.combat` 等引擎层。
- 不在 LLM 失败或 JSON 解析失败时回落到假 DM 文本。

## 方案一：普通 reply 拆成“决策 JSON + 叙述流”

### 当前流程

```text
perceive
  -> dm_decide
       LLM JSON: {"intent":"reply","say":"完整回复", ...}
  -> narrate_reply
       整段推送 say
  -> END
```

### 目标流程

```text
perceive
  -> dm_decide
       LLM JSON: {"intent":"reply","reply_brief":"回应意图/要点", ...}
  -> narrate_reply
       dm_narrate(...) 逐 token 生成玩家可见文本
  -> END
```

### 代码调整点

#### 1. 修改 `src.dm.world_bridge.decide_turn()`

`reply` 意图不再要求输出完整 `say`，改为输出结构化叙述计划，例如：

```json
{
  "intent": "reply",
  "reply_brief": "村长愿意透露失踪者线索，但想先确认玩家是否愿意帮忙。",
  "flags_set": {},
  "clues_delivered": []
}
```

规范化后返回：

```python
{
    "intent": "reply",
    "reply_brief": "...",
    "world_writes": writes,
}
```

保留对 reply 的校验，但校验对象从玩家可见文本变成 `reply_brief`：

- 必须非空。
- 不要求包含“你可以”，因为这会由叙述节点的 prompt 约束。

#### 2. 新增 `src.dm.world_bridge.narrate_reply_llm()`

新增 async 方法，负责真正生成玩家可见回复：

```python
async def narrate_reply_llm(
    reply_brief: str,
    scene: dict,
    *,
    user_input: str | None = None,
    messages: list[dict] | None = None,
    beat_brief: dict | None = None,
    stuck_hint: str | None = None,
    use_llm: bool,
    node_name: str = "dm",
) -> str:
    ...
    return await dm_narrate(task, node_name=node_name)
```

prompt 约束：

- 依据 `reply_brief`、当前场景、最近对话、当前剧情拍来写。
- 2-4 句，少铺陈。
- 不替玩家行动。
- 最后一句必须以“你可以”开头，给 2-3 个可选方向。
- 不输出 JSON，不罗列字段。

#### 3. 修改 `src.session.dm_subgraph.narrate_reply()`

从同步函数改成 async 函数：

```python
async def narrate_reply(state: DMState) -> dict:
    reply_brief = state.get("reply_brief", "") or ""
    text = await world_bridge.narrate_reply_llm(
        reply_brief,
        state.get("scene") or {},
        user_input=state.get("user_input"),
        messages=state.get("messages"),
        beat_brief=story_nodes.beat_brief_for(state),
        stuck_hint=story_nodes.stuck_hint_for(state),
        use_llm=llm_enabled(state),
    )
    messages = list(state.get("messages", []))
    messages.append({"role": "dm", "content": text})
    return {
        "messages": messages,
        "campaign_log": log_event(state, {"event": "narration", "text": text}),
    }
```

`dm_decide()` 的 reply 返回改为：

```python
return {
    "intent": intent,
    "reply_brief": decision.get("reply_brief", ""),
    "world_writes": writes,
    "next": "wait",
}
```

`perceive()` 清空工作区时新增：

```python
"reply_brief": "",
```

#### 4. `DMState` 字段补充

在 `src.model.dm_state.DMState` 增加：

```python
reply_brief: str  # reply 分支的叙述计划，由 DM 决策产生，玩家不可见
```

原有 `say` 可以暂时保留，因为 `SessionEngine._interpret()` 通过 messages 取最后一条 DM 文本，不依赖 `say`。是否删除 `say` 建议后续单独清理，避免一次变更扩大。

## 方案二：抽取 graph 流式消费公共方法

### 当前重复点

`src.session.engine.SessionEngine._astream_interpret()` 中这段逻辑具备通用性：

```python
async for mode, chunk in self._graph.astream(
    graph_input,
    config=config,
    stream_mode=["custom", "values"],
):
    if mode == "custom":
        if event_sink is not None:
            await event_sink(chunk)
    elif mode == "values":
        result = chunk
```

这段适合抽成公共工具，但不应该把 WebSocket 协议映射放进去。原因是引擎层和 common graph 工具不应该知道前端事件类型；`app.py` 才是 HTTP/WebSocket 边界。

### 新增文件

建议新增：

```text
src/common/utils/graph_stream.py
```

### 公共接口

```python
from collections.abc import Awaitable, Callable
from typing import Any

GraphStreamSink = Callable[[dict[str, Any]], Awaitable[None]]


async def astream_graph_values(
    graph: Any,
    graph_input: Any,
    *,
    config: dict,
    event_sink: GraphStreamSink | None = None,
) -> dict:
    """流式运行 LangGraph，转交 custom 事件并返回最后一个 values 状态。

    只负责 graph 层 custom/values 消费：
    - custom: 原样交给 event_sink
    - values: 保存最后一个状态

    不负责 WebSocket 协议映射，不解释业务 payload。
    """
```

行为约束：

- 固定使用 `stream_mode=["custom", "values"]`。
- 如果没有收到 values，抛 `RuntimeError`。
- `event_sink` 抛错时不吞掉异常，让调用方明确失败。
- 不对 chunk 做 JSON 编码；编码属于 app 边界。

### 修改 `SessionEngine`

`SessionStreamSink` 可以删掉，改用公共的 `GraphStreamSink`。

`_astream_interpret()` 简化为：

```python
result = await astream_graph_values(
    self._graph,
    graph_input,
    config={"configurable": {"thread_id": room_thread_id(room_id)}},
    event_sink=event_sink,
)
return self._interpret(room_id, result)
```

## 需要你确认的边界

1. 普通 `reply` 会从“一次 LLM 调用”变成“两次 LLM 调用”：第一次只做意图/世界写入 JSON，第二次流式生成玩家文本。这样体感更快，但总 token 和调用次数会上升。
2. `reply_brief` 是否允许包含少量“应传达线索/行动引导建议”。推荐允许，但玩家可见文本必须由第二次 `dm_narrate()` 生成。
3. 公共 graph stream 工具是否只处理 `custom/values`。推荐只做这一种，后续如需要 `messages/updates` 再新增函数，不让一个工具承担过多协议。
4. `say` 字段是否本轮删除。推荐暂时保留，等 reply 流式化稳定后再清理状态字段和旧注释。

## 实施步骤

1. 新增 `src/common/utils/graph_stream.py`，抽出 `astream_graph_values()`。
2. 修改 `SessionEngine._astream_interpret()` 使用公共工具。
3. 修改 `world_bridge._decide_llm()` 的 reply 输出要求，把 `say` 改成 `reply_brief`。
4. 修改 `world_bridge._normalize_decision()` 的 reply 规范化逻辑。
5. 新增 `world_bridge.narrate_reply_llm()`。
6. 修改 `session.dm_subgraph`：
   - `perceive()` 清空 `reply_brief`。
   - `dm_decide()` 写入 `reply_brief`。
   - `narrate_reply()` 改 async 并调用 `narrate_reply_llm()`。
7. 补 `DMState.reply_brief` 字段注释。
8. 运行验证：

```powershell
uv run python -m compileall src\common\utils\graph_stream.py src\session\engine.py src\session\dm_subgraph.py src\dm\world_bridge.py src\model\dm_state.py
npm run build
```

## 风险与回滚点

- 如果第二次 LLM 叙述不稳定，错误应直接暴露；不要回落到 `reply_brief` 拼模板。
- 如果成本或延迟不可接受，可以把 `reply_brief` prompt 压到极短，或者后续引入更小模型做决策，但不能恢复假 DM。
- 如果前端收到多个叙述节点，现有临时消息缓冲已经能保留多个临时段落，最终仍由 `session_update` 对齐。

