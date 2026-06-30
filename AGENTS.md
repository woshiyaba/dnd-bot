# Repository Guidelines

## Project Structure & Module Organization

This repository contains a Python backend and a Vite/React frontend. Backend code lives in `src/`: `src/app.py` exposes FastAPI routes, `src/graph.py` runs the agent graph, `src/combat/` and `src/session/` hold LangGraph state machines, `src/model/` contains domain dataclasses/enums, and `src/common/` contains shared utilities, prompts, agents, and WebSocket helpers. Frontend code is under `front/pc-dnd-bot/`. Domain content and design notes live in `docs/`, `knowledge/`, `canon/`, and `skills/`. Test and demo drivers are in `test/`.

Two backend layers currently live side by side:

- The FastAPI service is a deepagents + LangGraph scaffold. `POST /invoke` runs `src/graph.py`, which currently compiles a single `process` node around the cached `skills_find` deep agent and streams node/token events over WebSocket. The multi-agent Analyze/Strategy/SFTB/Wording/Polishing flow described in some docstrings is aspirational template text, not current behavior.
- The D&D combat engine in `src/combat/` and `src/model/` is an independent, interruptible LangGraph state machine. It is not wired into `src/app.py` or `src/graph.py` yet and is driven in code via `src.combat.engine.CombatEngine`, not HTTP.

## Build, Test, and Development Commands

- `uv sync`: install Python dependencies from `pyproject.toml` and `uv.lock`.
- `uv run python main.py`: start the FastAPI service on port `32388`.
- `uv run python -m src.common.example.example_agent`: invoke the `skills_find` deep agent directly for ad-hoc checks.
- `uv run python test/test_combat_flow.py`: run the standalone combat flow driver.
- `uv run python test/test_session_flow.py`: run the standalone session flow driver.
- `uv run python -m pytest test/test_combat_flow.py`: run pytest-compatible flow tests when pytest is installed.
- `cd front/pc-dnd-bot && npm run dev`: start the Vite frontend.
- `cd front/pc-dnd-bot && npm run build`: type-check and build the frontend.
- `cd front/pc-dnd-bot && npm run lint`: run `oxlint`.

The combat engine has no CLI entrypoint and is not reachable over HTTP. Drive it with `CombatEngine.start_combat()`, `CombatEngine.submit()`, and `CombatEngine.current_state()`. `main.py` may log `port=8000`, but the actual service bind is `0.0.0.0:32388`.

## Runtime Configuration

Copy `.env.example` for local configuration. The main backend agent needs DashScope/OpenAI-compatible model settings and MySQL prompt rows:

- `DASHSCOPE_API_KEY`, `DEFAULT_BASE_URL`, and optional `DEFAULT_MODEL` configure `ChatOpenAI` against Alibaba DashScope's OpenAI-compatible endpoint.
- `MYSQL_*` settings are required because system prompts are read from the `agent_system_prompts` table, not source files. No database means no usable main agent.
- `PROMPT_CACHE_TTL_SECONDS` controls prompt cache TTL; `0` queries the database every time.

Offline combat/session drivers can run without MySQL or DashScope when they stay on deterministic placeholder paths.

## Backend Architecture Notes

- `src/app.py` owns FastAPI routes. `POST /invoke` returns the final aggregated result, while incremental tokens and node lifecycle events are delivered out-of-band through `GET /ws/{user_id}`. Clients must connect the WebSocket first to observe streaming.
- `src/graph.py` streams the compiled graph with `stream_mode=["custom", "values"]`, maps custom events to WebSocket `node_start`, `stream`, and `node_end` messages, and currently uses an in-memory `MemorySaver` per call.
- `src/common/utils/writer.py` is the streaming bridge. Use `astream_agent_collect` inside graph nodes, passing `node_name` when lifecycle events should be emitted and `node_name=None` for standalone collection.
- `src/common/example/example_agent.py` builds and caches the `skills_find` deep agent. New agents should follow the create-once/cache-by-prompt pattern and rebuild only when the system prompt changes.
- `src/common/utils/llm_util.py` creates models and deepagents. Use `ReadOnlyFilesystemBackend` when an agent should read mounted knowledge/skills but must not mutate them.
- `src/common/prompts/prompt_repository.py` loads prompts from MySQL with TTL caching and single-flight locking. Adding an agent usually means adding a new `prompt_key` row.
- `src/common/utils/mysql_util.py` is the active async MySQL helper. `src/common/utils/db_util.py` is empty.
- `src/common/ws/ws_manager.py` contains the shared `ConnectionManager` singleton used by both routes and graph streaming.
- `skills/` contains deepagents skills as `SKILL.md` files with frontmatter (`name`, `description`) and is mounted read-only into the agent virtual filesystem.

## Combat Subsystem Notes

The combat subsystem implements the docs in `docs/战斗/` on top of `docs/原始数据.md`. Its guiding principle is: **规则归引擎，叙述归 DM，骰子归玩家**.

- Engine nodes perform deterministic Python resolution for hit, damage, HP, initiative, and victory checks. Do not let an LLM compute hits, damage, or HP.
- Player dice are collected through LangGraph `interrupt()` only for `is_player_controlled` combatants. Interrupt payloads include `directed_to.user_id` so the frontend can route rolls to the right player.
- Monster and environment dice are rolled by the engine with reproducible RNG in `src/combat/dice.py`, seedable through `scene_context["random_seed"]`.
- DM decisions and narration in `_dm_decide`/`narrate` are deterministic placeholders with hooks for a future LLM.
- `src/combat/graph.py` assembles the combat `StateGraph(CombatState)` and uses its own `MemorySaver` plus a serializer whitelist for model objects. Use a durable checkpointer for restartable multiplayer combat.
- `src/combat/engine.py` is the facade. One engine can serve many rooms, with fights keyed by `combat:{room_id}`.
- `src/combat/rules.py` holds pure judgment functions. Callers supply d20 values; rules never roll. Critical hit is d20 `20`, auto-miss is d20 `1`.
- `src/combat/interrupts.py` builds interrupt payloads and validates the trust boundary: raw d20 is clamped to 1-20 and modifiers are always added engine-side.
- `src/model/` holds domain dataclasses/enums only. `CombatState` is the single source of truth for combat state, and `combatants` stores model objects.

## Coding Style & Naming Conventions

Python targets 3.13 and uses Black (`[tool.black]`). Use English identifiers with PEP 8 casing: `snake_case` functions and variables, `PascalCase` classes, and `UPPER_SNAKE_CASE` constants. No Chinese or pinyin in identifiers; Chinese belongs in comments, docstrings, logs, and user-facing strings.

Existing backend comments, docstrings, logs, and narration are Chinese; match that convention for edited Python code. Public classes, functions, and methods should have Chinese docstrings that explain purpose, non-trivial parameters, and return values. In combat/model files, fields should keep inline Chinese domain comments where useful, such as `current_hp: int = 1  # 当前 HP`.

Keep model objects independent from graph/runtime layers: `src/model` should not import LangGraph or combat engine types, and `src/combat` depends on `src/model`, not the reverse. Keep deterministic rules in `rules.py`/`dice.py` rather than agent, graph, or UI code. Reuse `src/common/utils/json_parser.py` (`extract_json_object`, `extract_json_array`) for defensive LLM JSON parsing instead of bare `json.loads` on model output.

Enum values are lowercase English strings and often persist or travel over the wire. Changing enum values is a data-format change that must be coordinated with docs and frontend code.

Logging is configured with `ensure_logging_config()` in entrypoints. Use `get_elapsed_ms(start_time)` for timing and follow the existing `[step_name] ... | key=value` log style. Prefer async paths for DB access, agent streaming, and WebSocket I/O.

## Testing Guidelines

There is no fully configured test suite yet. Treat `test/test_combat_flow.py` and `test/test_session_flow.py` as executable flow checks; they are the safest regression checks for combat/session work. `test/test_story_flow.py` is an interactive CLI requiring a real model key, and `test/dp.py` is a standalone deepagents example that can make live model calls at import time, so do not import or include it in routine automated runs.

## Commit & Pull Request Guidelines

Recent commits use Conventional Commit-style prefixes such as `feat:`, `fix:`, and `style:` with short Chinese summaries. Follow that pattern and keep each commit focused. Pull requests should describe the behavior change, list verification commands run, note required `.env` or database changes, and include screenshots for frontend UI changes.

## Security & Configuration Tips

Do not commit API keys, database credentials, generated virtual environments, or build output. Keep agent filesystem access read-only when the use case only requires reading knowledge, canon, or skills.

## Stale or Placeholder Code

`src/common/agents/main_agent.py`, `src/common/agents/router.py`, and `src/common/utils/db_util.py` are empty placeholders. Some docstrings in `src/graph.py` and `src/app.py` describe a multi-agent pipeline that is not implemented; rely on the actual code paths above when reasoning about behavior.
