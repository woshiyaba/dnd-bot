import os
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from dotenv import load_dotenv
from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import EditResult, FileUploadResponse, WriteResult
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver

load_dotenv()
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "qwen3.5-plus")
DEFAULT_BASE_URL = os.getenv("DEFAULT_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
DEFAULT_API_KEY_ENV = "DASHSCOPE_API_KEY"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SKILLS_DIR = PROJECT_ROOT / "skills"

MODEL_PRESETS: dict[str, dict[str, Any]] = {
    "default": {},
    "analyze": {},
    "strategy": {},
}

AUTO_SKILLS = object()


class ReadOnlyFilesystemBackend(FilesystemBackend):
    """允许读取本地知识库，但拒绝任何写入操作。"""

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=f"禁止写入文件：{file_path}。请直接返回结果，不要保存到本地文件。")

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return EditResult(error=f"禁止修改文件：{file_path}。请直接返回结果，不要保存到本地文件。")

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [
            FileUploadResponse(path=path, error="permission_denied")
            for path, _ in files
        ]


def _to_backend_dir(path: Path, root: Path) -> str:
    relative = path.resolve().relative_to(root.resolve())
    return f"/{relative.as_posix().strip('/')}/"


def _resolve_skill_sources(project_root: Path, skills_dir: Path | None) -> list[str] | None:
    if skills_dir is None or not skills_dir.is_dir():
        return None

    return [_to_backend_dir(skills_dir, project_root)]

def create_chat_model(
    model: str = DEFAULT_MODEL,
    *,
    base_url: str = DEFAULT_BASE_URL,
    api_key: str | None = None,
    enable_search: bool = True,
    extra_body: dict[str, Any] | None = None,
    **kwargs: Any,
) -> ChatOpenAI:
    resolved_api_key = api_key or os.getenv(DEFAULT_API_KEY_ENV)
    merged_extra_body: dict[str, Any] = {}

    if enable_search:
        pass
        # merged_extra_body["enable_search"] = True

    if extra_body:
        merged_extra_body.update(extra_body)

    init_kwargs: dict[str, Any] = {
        "model": model,
        "base_url": base_url,
        **kwargs,
    }

    if resolved_api_key:
        init_kwargs["api_key"] = resolved_api_key

    if merged_extra_body:
        init_kwargs["extra_body"] = merged_extra_body

    return ChatOpenAI(**init_kwargs)


def create_app_deep_agent(
    *,
    system_prompt: str,
    model: Any | None = None,
    project_root: Path = PROJECT_ROOT,
    skills_dir: Path | None = DEFAULT_SKILLS_DIR,
    skills: list[str] | None | object = AUTO_SKILLS,
    backend: FilesystemBackend | None = None,
    checkpointer: Any | None = None,
    chat_model_kwargs: dict[str, Any] | None = None,
    **agent_kwargs: Any,
) -> Any:
    resolved_model = model or create_chat_model(**(chat_model_kwargs or {}))
    resolved_backend = backend or FilesystemBackend(root_dir=project_root, virtual_mode=True)
    resolved_skills = (
        _resolve_skill_sources(project_root, skills_dir)
        if skills is AUTO_SKILLS
        else skills
    )
    resolved_checkpointer = checkpointer or MemorySaver()

    return create_deep_agent(
        model=resolved_model,
        backend=resolved_backend,
        skills=resolved_skills,
        system_prompt=system_prompt,
        checkpointer=resolved_checkpointer,
        **agent_kwargs,
    )
