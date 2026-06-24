# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **deepagents + LangGraph template** exposed as a FastAPI service. Despite the `dnd-bot` name, the code is a generic scaffold for building agentic flows: a LangGraph graph wraps a cached "deep agent", streams its tokens over WebSocket, and loads system prompts from a MySQL table. The graph currently runs a single `process` node that drives a skill-discovery agent — `src/graph.py`'s module docstring describes a multi-phase parallel pipeline that is **aspirational/template text, not the implemented flow**.

## Commands

The project uses `uv` (see `uv.lock`, `requires-python >=3.13`).

```bash
uv sync                  # install dependencies into .venv
uv run python main.py    # start the FastAPI server (binds 0.0.0.0:32388)
uv run python -m src.common.agents.example_agent   # ad-hoc: invoke the skills_find agent directly (see example_agent.main)
```

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

### Placeholders / stale code to be aware of
`src/common/agents/main_agent.py`, `src/common/agents/router.py`, and `src/common/utils/db_util.py` are empty. `src/graph.py`'s docstring and `app.py`'s `/invoke` docstring describe an Analyze/Strategy/SFTB/Wording/Polishing multi-agent pipeline that does not exist in code — ignore it when reasoning about current behavior.

## Conventions

- Code, comments, docstrings, and log messages are in **Chinese**; match this when editing.
- Logging is set up via `ensure_logging_config()` (call it at module load in entrypoints); use `get_elapsed_ms(start_time)` for timing. Log lines follow `[step_name] ... | key=value` formatting (see helpers in `graph.py`).
- Async throughout: DB access, agent streaming, and WebSocket I/O are all `async`. Prefer the `astream_*` paths over the sync `stream_*` variants.
- LLM output is parsed defensively with `src/common/utils/json_parser.py` (`extract_json_object` / `extract_json_array`) which strip markdown fences and surrounding prose — reuse these instead of bare `json.loads` on model output.
