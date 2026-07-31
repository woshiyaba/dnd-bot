import logging
import os
import re
import threading
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import EditResult, FileUploadResponse, WriteResult
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SKILLS_DIR = PROJECT_ROOT / "skills"

AUTO_SKILLS = object()
_PROVIDER_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")


class LLMConfigurationError(RuntimeError):
    """LLM 供应商、模型目录或职责映射配置不合法。"""


class ModelRole(StrEnum):
    """应用内所有可独立选择模型的 LLM 职责。"""

    DM_DECISION = "dm_decision"
    DM_GUIDANCE = "dm_guidance"
    DM_TRIGGER = "dm_trigger"
    DM_NARRATION = "dm_narration"
    COMBAT_DECISION = "combat_decision"
    COMBAT_NARRATION = "combat_narration"
    ACTION_COMPILER = "action_compiler"
    STORY_INTERVIEW = "story_interview"
    STORY_AUTHORING = "story_authoring"
    STORY_REPAIR = "story_repair"
    LEGACY_AGENT = "legacy_agent"


_ROLE_ENV_NAMES: dict[ModelRole, str] = {
    ModelRole.DM_DECISION: "DM_DECISION_MODEL",
    ModelRole.DM_GUIDANCE: "DM_GUIDANCE_MODEL",
    ModelRole.DM_TRIGGER: "DM_TRIGGER_MODEL",
    ModelRole.DM_NARRATION: "DM_NARRATION_MODEL",
    ModelRole.COMBAT_DECISION: "COMBAT_DECISION_MODEL",
    ModelRole.COMBAT_NARRATION: "COMBAT_NARRATION_MODEL",
    ModelRole.ACTION_COMPILER: "ACTION_COMPILER_MODEL",
    ModelRole.STORY_INTERVIEW: "STORY_INTERVIEW_MODEL",
    ModelRole.STORY_AUTHORING: "STORY_AUTHORING_MODEL",
    ModelRole.STORY_REPAIR: "STORY_REPAIR_MODEL",
    ModelRole.LEGACY_AGENT: "LEGACY_AGENT_MODEL",
}
_REASONING_ROLES = {
    ModelRole.DM_DECISION,
    ModelRole.STORY_AUTHORING,
    ModelRole.STORY_REPAIR,
}


@dataclass(frozen=True)
class ProviderConfig:
    """一个 OpenAI 兼容模型供应商的连接配置。"""

    name: str
    base_url: str
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class ModelConfig:
    """一个通过 ``供应商/上游模型 ID`` 唯一标识的模型。"""

    name: str
    provider_name: str
    model_id: str


@dataclass
class ModelRegistry:
    """启动时构建的模型客户端与职责映射。"""

    providers: dict[str, ProviderConfig]
    models: dict[str, ModelConfig]
    clients: dict[str, ChatOpenAI]
    role_models: dict[ModelRole, str]

    def get_model(self, model_name: str) -> ChatOpenAI:
        """按已登记的复合名返回同一个缓存客户端。"""
        normalized = str(model_name or "").strip()
        try:
            return self.clients[normalized]
        except KeyError as exc:
            raise LLMConfigurationError(
                f"模型 «{normalized or '空'}» 未登记在 LLM_MODELS"
            ) from exc

    def model_name_for(self, role: ModelRole | str) -> str:
        """返回职责当前绑定的模型复合名。"""
        try:
            normalized_role = role if isinstance(role, ModelRole) else ModelRole(role)
        except ValueError as exc:
            raise LLMConfigurationError(f"未知 LLM 职责：{role!r}") from exc
        return self.role_models[normalized_role]


_registry_lock = threading.Lock()
_registry: ModelRegistry | None = None


def _split_csv(value: str | None, *, env_name: str) -> list[str]:
    items = [item.strip() for item in str(value or "").split(",") if item.strip()]
    if not items:
        raise LLMConfigurationError(f"{env_name} 不能为空")
    if len(items) != len(set(items)):
        raise LLMConfigurationError(f"{env_name} 含重复项")
    return items


def _provider_env_suffix(provider_name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", provider_name.upper())


def _require_env(environ: Mapping[str, str], name: str) -> str:
    value = str(environ.get(name) or "").strip()
    if not value:
        raise LLMConfigurationError(f"缺少必要的模型配置：{name}")
    return value


def _validate_base_url(value: str, *, env_name: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise LLMConfigurationError(f"{env_name} 必须是合法的 HTTP(S) 地址")
    return value.rstrip("/")


def _parse_provider_configs(
    environ: Mapping[str, str],
) -> dict[str, ProviderConfig]:
    names = _split_csv(environ.get("LLM_PROVIDERS"), env_name="LLM_PROVIDERS")
    suffixes: dict[str, str] = {}
    providers: dict[str, ProviderConfig] = {}

    for name in names:
        if not _PROVIDER_NAME_PATTERN.fullmatch(name):
            raise LLMConfigurationError(
                f"供应商名称 «{name}» 只能使用小写字母、数字、下划线和连字符"
            )
        suffix = _provider_env_suffix(name)
        if suffix in suffixes:
            raise LLMConfigurationError(
                f"供应商 «{name}» 与 «{suffixes[suffix]}» 的环境变量前缀冲突"
            )
        suffixes[suffix] = name
        base_url_env = f"LLM_PROVIDER_{suffix}_BASE_URL"
        api_key_env = f"LLM_PROVIDER_{suffix}_API_KEY"
        providers[name] = ProviderConfig(
            name=name,
            base_url=_validate_base_url(
                _require_env(environ, base_url_env),
                env_name=base_url_env,
            ),
            api_key=_require_env(environ, api_key_env),
        )
    return providers


def _parse_model_configs(
    environ: Mapping[str, str],
    providers: Mapping[str, ProviderConfig],
) -> dict[str, ModelConfig]:
    names = _split_csv(environ.get("LLM_MODELS"), env_name="LLM_MODELS")
    models: dict[str, ModelConfig] = {}
    for name in names:
        provider_name, separator, model_id = name.partition("/")
        if (
            not separator
            or not provider_name
            or not model_id
            or any(char.isspace() for char in name)
        ):
            raise LLMConfigurationError(
                f"模型 «{name}» 必须使用 «供应商/模型 ID» 复合名"
            )
        if provider_name not in providers:
            raise LLMConfigurationError(
                f"模型 «{name}» 引用了未登记供应商 «{provider_name}»"
            )
        models[name] = ModelConfig(
            name=name,
            provider_name=provider_name,
            model_id=model_id,
        )
    return models


def _parse_role_models(
    environ: Mapping[str, str],
    models: Mapping[str, ModelConfig],
) -> dict[ModelRole, str]:
    reasoning_model = _require_env(environ, "LLM_REASONING_MODEL")
    fast_model = _require_env(environ, "LLM_FAST_MODEL")
    for env_name, model_name in (
        ("LLM_REASONING_MODEL", reasoning_model),
        ("LLM_FAST_MODEL", fast_model),
    ):
        if model_name not in models:
            raise LLMConfigurationError(f"{env_name} 引用了未登记模型 «{model_name}»")

    resolved: dict[ModelRole, str] = {}
    for role, env_name in _ROLE_ENV_NAMES.items():
        default_model = reasoning_model if role in _REASONING_ROLES else fast_model
        model_name = str(environ.get(env_name) or default_model).strip()
        if model_name not in models:
            raise LLMConfigurationError(f"{env_name} 引用了未登记模型 «{model_name}»")
        resolved[role] = model_name
    return resolved


def build_model_registry(
    environ: Mapping[str, str],
    *,
    model_factory: Callable[..., ChatOpenAI] | None = None,
) -> ModelRegistry:
    """从给定环境映射构造注册表；不读取磁盘，也不发送模型请求。"""
    resolved_factory = model_factory or ChatOpenAI
    providers = _parse_provider_configs(environ)
    models = _parse_model_configs(environ, providers)
    role_models = _parse_role_models(environ, models)
    clients: dict[str, ChatOpenAI] = {}
    for name, model in models.items():
        provider = providers[model.provider_name]
        try:
            clients[name] = resolved_factory(
                model=model.model_id,
                base_url=provider.base_url,
                api_key=provider.api_key,
            )
        except Exception as exc:
            raise LLMConfigurationError(
                f"模型 «{name}» 客户端初始化失败（{type(exc).__name__}）"
            ) from None
    return ModelRegistry(
        providers=providers,
        models=models,
        clients=clients,
        role_models=role_models,
    )


def initialize_model_registry() -> ModelRegistry:
    """加载 ``.env``，本地校验并缓存全部已登记模型客户端。"""
    global _registry
    if _registry is not None:
        return _registry
    with _registry_lock:
        if _registry is not None:
            return _registry
        load_dotenv()
        registry = build_model_registry(os.environ)
        _registry = registry
        logger.info(
            "[llm_registry] 初始化完成 | providers=%s | models=%s | roles=%s",
            sorted(registry.providers),
            sorted(registry.models),
            {
                role.value: model_name
                for role, model_name in registry.role_models.items()
            },
        )
        return registry


def get_chat_model(model_name: str) -> ChatOpenAI:
    """按复合名选择启动时登记的聊天模型。"""
    return initialize_model_registry().get_model(model_name)


def get_model_name(role: ModelRole | str) -> str:
    """返回指定任务职责绑定的模型复合名。"""
    return initialize_model_registry().model_name_for(role)


def _reset_model_registry_for_tests() -> None:
    """清空进程内模型注册表，仅供离线测试隔离全局状态。"""
    global _registry
    with _registry_lock:
        _registry = None


class ReadOnlyFilesystemBackend(FilesystemBackend):
    """允许读取本地知识库，但拒绝任何写入操作。"""

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(
            error=f"禁止写入文件：{file_path}。请直接返回结果，不要保存到本地文件。"
        )

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return EditResult(
            error=f"禁止修改文件：{file_path}。请直接返回结果，不要保存到本地文件。"
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [
            FileUploadResponse(path=path, error="permission_denied")
            for path, _ in files
        ]


def _to_backend_dir(path: Path, root: Path) -> str:
    relative = path.resolve().relative_to(root.resolve())
    return f"/{relative.as_posix().strip('/')}/"


def _resolve_skill_sources(
    project_root: Path, skills_dir: Path | None
) -> list[str] | None:
    if skills_dir is None or not skills_dir.is_dir():
        return None

    return [_to_backend_dir(skills_dir, project_root)]


def create_app_deep_agent(
    *,
    system_prompt: str,
    model: Any | None = None,
    model_name: str | None = None,
    project_root: Path = PROJECT_ROOT,
    skills_dir: Path | None = DEFAULT_SKILLS_DIR,
    skills: list[str] | None | object = AUTO_SKILLS,
    backend: FilesystemBackend | None = None,
    checkpointer: Any | None = None,
    **agent_kwargs: Any,
) -> Any:
    if model is not None and model_name is not None:
        raise ValueError("model 与 model_name 不能同时提供")
    resolved_model = model or get_chat_model(
        model_name or get_model_name(ModelRole.LEGACY_AGENT)
    )
    resolved_backend = backend or FilesystemBackend(
        root_dir=project_root, virtual_mode=True
    )
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
