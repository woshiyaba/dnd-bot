# Repository Guidelines

## Project Structure & Module Organization

Backend code lives in `src/`. `src/app.py` mounts `src/api/` routers, backed by `src/schemas/` contracts and `src/services/`. Gameplay runs through `SessionEngine` in `src/session/`, embedding the LLM DM (`src/dm/`), combat (`src/combat/`), canon (`src/story/`), character rules (`src/character/`), and models (`src/model/`). `src/graph.py` and `POST /invoke` are a separate legacy deepagents example.

Clients live in `front/pc-dnd-bot/` (React/Vite) and `front/mini-app/` (native TypeScript WeChat). Adventures, game data, rules, notes, and checks live in `canon/`, `dnd_skill/`, `knowledge/`, `docs/`, and `test/`.

## Build, Test, and Development Commands

- `uv sync`: install locked Python dependencies.
- `uv run python main.py [--debug]`: start FastAPI on port `32388`.
- `uv run python -m unittest discover -s test -p "test_*.py"`: run offline unit and contract tests.
- `uv run python test/test_combat_flow.py` or `test/test_session_flow.py`: run live-model flows.
- In `front/pc-dnd-bot`, use `npm ci`, `npm run dev`, `npm run build`, and `npm run lint`.
- In `front/mini-app`, use `npm ci` and `npm run type-check`, then import the directory into WeChat Developer Tools.

## Runtime Configuration & Safety

Copy `.env.example` to `.env`. LLM paths use `DASHSCOPE_API_KEY`, `DEFAULT_BASE_URL`, `DEFAULT_MODEL`, and `STORY_GENERATION_MODEL`. Legacy `/invoke` also needs `MYSQL_*` variables and `PROMPT_CACHE_TTL_SECONDS`.

Rooms and LangGraph checkpoints are in memory; restarts lose them. Never commit credentials, `.env`, virtual environments, `node_modules`, or build output.

DM decisions and narration must use a real LLM and fail visibly on configuration, call, or parse errors. Never add mock, heuristic, template, or offline-DM fallbacks. Hit, damage, HP, initiative, and checks belong to the engine.

## Coding Style & Naming Conventions

Python targets 3.13 with Black formatting. Use English `snake_case`, `PascalCase`, and `UPPER_SNAKE_CASE` identifiers; use Chinese for comments, public docstrings, logs, and user-facing text. TypeScript is strict and follows the existing two-space style.

Keep `src/model/` independent of FastAPI/LangGraph. Put calculations in rule modules, orchestration in graphs/services, and boundary validation in schemas or interrupt handlers. Parse LLM JSON with `src/common/utils/json_parser.py`. Lowercase enum values are wire formats; coordinate backend, frontend, tests, and docs when changing them.

## Testing Guidelines

Name files `test_*.py` and methods `test_*`. Prefer deterministic `unittest` coverage with patched LLM boundaries. `test/test_story_flow.py` is interactive and `test/dp.py` may call a live model, so exclude both from routine automation. No coverage threshold is enforced.

## Commit & Pull Request Guidelines

History uses prefixes such as `feat:`, `fix:`, `style:`, and `front:` with short Chinese summaries. PRs should explain impact, list verification, note environment or wire-format changes, link issues, and include UI screenshots.

## 代码风格
风格简介易懂，以实现逻辑为主，不做过多的兜底