# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Two layers live side by side in this repo:

1. **A deepagents + LangGraph template** exposed as a FastAPI service — a generic scaffold for agentic flows: a LangGraph graph wraps a cached "deep agent", streams its tokens over WebSocket, and loads system prompts from a MySQL table. The graph currently runs a single `process` node that drives a skill-discovery agent — `src/graph.py`'s module docstring describes a multi-phase parallel pipeline that is **aspirational/template text, not the implemented flow**.

2. **A D&D combat engine** (`src/combat/` + `src/model/`) — the first real domain feature, a self-contained, interruptible, persistable LangGraph state machine that runs a turn-based fight. **It is currently standalone: nothing in `app.py` or `src/graph.py` imports or wires it in yet.** Driven via the `CombatEngine` façade, not over HTTP. See the dedicated section below.

## Commands

The project uses `uv` (see `uv.lock`, `requires-python >=3.13`).

```bash
uv sync                  # install dependencies into .venv
uv run python main.py    # start the FastAPI server (binds 0.0.0.0:32388)
uv run python -m src.common.example.example_agent   # ad-hoc: invoke the skills_find agent directly (see example_agent.main)
```

The combat engine has **no CLI entrypoint and is not reachable over HTTP** — drive it in code via `src.combat.engine.CombatEngine` (`start_combat` → `submit` → `current_state`). It needs neither MySQL nor DashScope (DM/narration are deterministic placeholders), so it runs standalone.

There is no configured linter, formatter, or test runner. `test/dp.py` is **not** a pytest test — it is a standalone deepagents example that calls a Gemini model and runs `agent.invoke(...)` at import time, so importing it makes live model calls. Do not treat `test/` as a runnable suite.

Note: `main.py` logs `port=8000` but actually binds **32388** (the log string is stale).

## Required environment (.env)

Copy `.env.example`. The service will not start a request successfully without:
- `DASHSCOPE_API_KEY` + `DEFAULT_BASE_URL` — models go through Alibaba DashScope's **OpenAI-compatible** endpoint via `ChatOpenAI` (default model `qwen3.5-plus`, overridable with `DEFAULT_MODEL`).
- `MYSQL_*` — system prompts are read from MySQL, **not** from files (see below). No DB = no usable agent.
- `PROMPT_CACHE_TTL_SECONDS` — TTL for the prompt cache; `0` means query the DB every time.

## Architecture

Request/stream flow for `POST /invoke`:

```
app.py  →  graph.invoke()  →  StateGraph(process)  →  example_agent (deep agent)
   │                                  │                        │
   │  ws "flow_start"/"flow_end"      │ custom stream events    │ messages tokens
   └──────────────────────────────────┴──── ws_manager ────────┘ → frontend WebSocket
```

- **`src/app.py`** — FastAPI app. `POST /invoke` runs the graph; `GET /ws/{user_id}` opens a per-user WebSocket. CORS is wide open. The HTTP response is the final aggregated result; **incremental tokens and node lifecycle are delivered out-of-band over the WebSocket**, keyed by `user_id`. A client must connect to `/ws/{user_id}` first to see streaming.

- **`src/graph.py`** — LangGraph orchestration. `invoke()` streams the compiled graph with `stream_mode=["custom","values"]`, translates `custom` events into `node_start`/`stream`/`node_end` WebSocket messages, and returns the final `values`. The graph is compiled fresh per call with an in-memory `MemorySaver` checkpointer (thread ids are currently hard-coded, e.g. `"thread_123"`).

- **`src/common/utils/writer.py`** — the streaming bridge. `astream_agent_collect` subscribes to both `messages` (token stream → pushed live) and `updates` (final `AIMessage` → collected as the result), driven by `StreamCollector`, which emits `{node,status,chunk}` `custom` events through LangGraph's `get_stream_writer()`. This is the key piece connecting an agent's internal stream to the graph's `custom` event channel. Use `astream_agent_collect` for in-graph nodes (pass `node_name` to emit lifecycle events); pass `node_name=None` for standalone calls that only need the collected text.

- **`src/common/example/example_agent.py`** — builds and **caches** the `skills_find` deep agent. The agent is rebuilt only when its system prompt changes (double-checked under an `asyncio.Lock`). New agents should follow this create-once/cache-by-prompt pattern.

- **`src/common/utils/llm_util.py`** — factories. `create_chat_model` wires DashScope/Qwen. `create_app_deep_agent` assembles a deepagents agent with a `FilesystemBackend` and the `skills/` directory mounted as skill sources. `ReadOnlyFilesystemBackend` overrides `write`/`edit`/`upload_files` to hard-deny mutations (returns error results telling the model to return text instead of writing files) — use it whenever the agent should read the knowledge base but never modify it.

- **`src/common/prompts/prompt_repository.py`** — `get_system_prompt(key)` loads `prompt_content` from MySQL table `agent_system_prompts` (`WHERE prompt_key=%s AND enabled=1`) with a short TTL cache and single-flight locking. **System prompts live in the database, not in source.** Adding an agent means inserting a row with a new `prompt_key` (e.g. the agent's `PROMPT_KEY`).

- **`src/common/utils/mysql_util.py`** — lazy global `aiomysql` pool + `fetch_one`. **`db_util.py` is empty; use `mysql_util`.**

- **`src/common/ws/ws_manager.py`** — `ConnectionManager` mapping `user_id → [WebSocket]` (multiple connections per user). The module-level `manager` singleton is shared by `app.py` and `graph.py`.

- **`skills/`** — deepagents "skills" as `SKILL.md` files with frontmatter (`name`, `description`). Mounted read-only into the agent's virtual filesystem so the model can discover and read them.

### Combat subsystem (`src/combat/` + `src/model/`)

A separate LangGraph state machine for turn-based D&D fights, implementing the spec in `docs/战斗/` (`01` state, `02` flow, `03` interrupt protocol) on top of the global rules in `docs/原始数据.md`. **Independent from the deepagents template above** — different graph, different state, different checkpointer; they don't call each other yet.

Guiding principle (from `docs/战斗/README.md`): **规则归引擎，叙述归 DM，骰子归玩家** — rules belong to the engine, narration to the DM, dice to the player.
- **Engine nodes** = pure-Python deterministic resolution (hit/damage/HP/initiative/win-check). Never let an LLM compute a hit or track HP.
- **Player dice** = collected via LangGraph `interrupt()`, but **only for `is_player_controlled` combatants**; the interrupt payload carries `directed_to.user_id` so the front end can route "who rolls what" to the right player.
- **Monster/environment dice** = rolled by the engine from a **reproducible** RNG (`src/combat/dice.py`, seedable via `scene_context["random_seed"]`).
- **DM decisions/narration** (`_dm_decide`, `narrate`) are deterministic heuristic **placeholders** today, with hooks left to swap in an LLM later.

Layout:
- **`src/combat/graph.py`** — assembles `StateGraph(CombatState)`: `enter_combat → judge_surprise → roll_initiative → next_turn → declare_action → resolve_action → narrate → check_end`, where `check_end` loops back to `next_turn` while in progress else `settle → END`. Compiles with its **own** `MemorySaver` using a custom `JsonPlusSerializer` whitelist (`_COMBAT_SERDE_WHITELIST`) so the combat model objects survive msgpack persistence. Pass a real checkpointer (SQLite/MySQL) for multi-player/restart durability.
- **`src/combat/engine.py`** — `CombatEngine` façade. One engine serves many rooms; each fight is keyed by a unique `thread_id` (`combat:{room_id}`). `start_combat`/`submit` call `ainvoke`/`Command(resume=…)` and normalize results to `{"status": "interrupted"|"finished", …}`.
- **`src/combat/nodes.py`** — the node bodies. Turn-start settlement (DoT, condition decrement, skip surprised/stunned) happens at the top of `next_turn` so the graph keeps a single back-edge. `resolve_action` dispatches by `action_type` (`attack`/`skill`/`item`/`improvise`/`move`/`pass`) into `_resolve_*` helpers that emit structured events; `narrate` turns events into prose and pushes them through `get_stream_writer()` (reusing the same `custom` event channel as `graph.py`).
- **`src/combat/rules.py`** — pure judgment functions (`resolve_attack`, `check_success`, `in_reach`, proficiency math). Crit on d20==20 (auto-hit + double damage dice), auto-miss on d20==1. Callers supply the d20; rules never roll.
- **`src/combat/interrupts.py`** — builds interrupt request payloads and the legal-action options for the declare step; `validate_d20`/`extract_damage` enforce the trust boundary (raw d20 clamped to 1–20, **all modifiers added engine-side**).
- **`src/model/`** — domain dataclasses with **English identifiers and Chinese comments** (the inline comment names the original domain term, e.g. `name  # 名字`). `combatant.py` is an inheritance tree: `Combatant`(base = minimal monster card) → `Monster`, and `Combatant` → `Character` → {`PlayerCharacter`, `NPC`}; shared combat fields live once on `Combatant`, richer card fields are added at `Character`. `enums.py` are `StrEnum`s whose **values are lowercase English strings** that get persisted/sent on the wire (`Ability` member values must match the `Combatant` ability field names, since `modifier()` does `getattr(self, ability.value)`). `combat_state.py` defines the `CombatState` TypedDict (the single source of truth — `combatants` holds the model objects) plus `load_combatants` which builds combatants from `scene_context["combatants"]` card entries.

### Placeholders / stale code to be aware of
`src/common/agents/main_agent.py`, `src/common/agents/router.py`, and `src/common/utils/db_util.py` are empty. `src/graph.py`'s docstring and `app.py`'s `/invoke` docstring describe an Analyze/Strategy/SFTB/Wording/Polishing multi-agent pipeline that does not exist in code — ignore it when reasoning about current behavior.

## Conventions

- Comments, docstrings, log messages, and user-facing narration are in **Chinese**; match this when editing. **Identifiers are English** (the codebase was migrated off Chinese identifiers) — in the combat/model layer, give each renamed field/method an inline Chinese comment naming the domain term (e.g. `current_hp: int = 1  # 当前 HP`). Enum **values** are lowercase English strings and double as the on-the-wire/DB representation, so changing a value is a data-format change (update `docs/战斗/03` and any frontend together).

### Coding standards (apply to all new/edited code)
- **English identifiers, PEP 8 casing.** Variables, functions, methods → `snake_case`; classes → `PascalCase`; module-level constants → `UPPER_SNAKE_CASE`. No Chinese (or pinyin) in identifiers — Chinese belongs only in comments/docstrings/strings.
- **Comment every public class, method, function, and its parameters/return in Chinese.** Use a docstring on classes/functions (state what it does, and what each non-trivial parameter and the return value mean); use inline `# 中文` on fields and on any non-obvious line. A reader should understand intent without reading the body.
- **Encapsulate reusable logic.** Don't copy-paste a calculation or a multi-line block twice — extract a well-named helper (see how `rules.py` centralizes the d20-vs-DC math, `dice.py` the rolling, `interrupts.py` the payload building). Pure deterministic logic stays in `rules.py`/`dice.py`, free of graph state.
- **Keep files small and single-responsibility.** One concern per file; if a module starts mixing concerns or grows past a few hundred lines, split it. Follow the existing split: `model/` = data shapes, `combat/rules|dice` = pure rules, `combat/nodes` = graph nodes, `combat/interrupts` = player protocol, `combat/engine|graph` = assembly/façade. Put new code in the file whose responsibility it matches, or add a new file rather than overloading an existing one.
- **Packages have clear boundaries.** `src/model` knows nothing about LangGraph; `src/combat` depends on `src/model`, not the reverse. Keep that direction — don't import graph/engine types into the model layer.
- Logging is set up via `ensure_logging_config()` (call it at module load in entrypoints); use `get_elapsed_ms(start_time)` for timing. Log lines follow `[step_name] ... | key=value` formatting (see helpers in `graph.py`).
- Async throughout: DB access, agent streaming, and WebSocket I/O are all `async`. Prefer the `astream_*` paths over the sync `stream_*` variants.
- LLM output is parsed defensively with `src/common/utils/json_parser.py` (`extract_json_object` / `extract_json_array`) which strip markdown fences and surrounding prose — reuse these instead of bare `json.loads` on model output.
