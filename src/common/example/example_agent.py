import asyncio
from pathlib import Path
from typing import Any

from src.common.prompts.prompt_repository import get_system_prompt
from src.common.utils.llm_util import ReadOnlyFilesystemBackend, create_app_deep_agent
from src.common.utils.writer import astream_agent_collect

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SKILLS_DIR = PROJECT_ROOT / "skills"
PROMPT_KEY = "skills_find"

_agent_lock = asyncio.Lock()
_cached_agent: Any | None = None
_cached_system_prompt: str | None = None


def _build_skills_find_agent(system_prompt: str) -> Any:
    return create_app_deep_agent(
        system_prompt=system_prompt,
        skills_dir=SKILLS_DIR,
        backend=ReadOnlyFilesystemBackend(root_dir=PROJECT_ROOT, virtual_mode=True),
    )


async def create_skills_find_agent() -> Any:
    """复用 skills_find agent；仅当数据库提示词变化后才重建。"""
    global _cached_agent, _cached_system_prompt

    system_prompt = await get_system_prompt(PROMPT_KEY)
    if _cached_agent is not None and _cached_system_prompt == system_prompt:
        return _cached_agent

    # 防止并发请求同时发现 prompt 变化后重复创建 deep agent。
    async with _agent_lock:
        if _cached_agent is not None and _cached_system_prompt == system_prompt:
            return _cached_agent

        _cached_agent = _build_skills_find_agent(system_prompt)
        _cached_system_prompt = system_prompt
        return _cached_agent


async def build_user_input(
        skill_name: str,
) -> str:
    """组装鼓励话语生成的输入 prompt。"""
    parts = f"帮我下载并安装：{skill_name} skills"
    return parts


async def main():
    """流式调用 agent 并收集完整结果（异步版）。

    同时订阅 messages（流式推送思考过程）和 updates（提取最终结果）。
    node_name 非空时同时推送 start/streaming/end 状态事件。
    """
    agent = await create_skills_find_agent()
    raw_content = await astream_agent_collect(
        agent,
        await build_user_input("frontend-design"),
        thread_id="thread_123",
        node_name=PROMPT_KEY,
    )
    return raw_content


if __name__ == '__main__':
    asyncio.run(main())
