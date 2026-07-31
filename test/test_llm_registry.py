"""多供应商模型目录、职责映射与启动初始化的离线测试。"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.app import app
from src.common.utils import llm_util
from src.common.utils.llm_util import (
    LLMConfigurationError,
    ModelRole,
    build_model_registry,
)


def _environment() -> dict[str, str]:
    return {
        "LLM_PROVIDERS": "deepseek,dashscope",
        "LLM_PROVIDER_DEEPSEEK_BASE_URL": "https://api.deepseek.com/",
        "LLM_PROVIDER_DEEPSEEK_API_KEY": "deepseek-secret",
        "LLM_PROVIDER_DASHSCOPE_BASE_URL": (
            "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ),
        "LLM_PROVIDER_DASHSCOPE_API_KEY": "dashscope-secret",
        "LLM_MODELS": (
            "deepseek/deepseek-v4-pro,"
            "deepseek/deepseek-v4-flash,"
            "dashscope/qwen3.5-plus"
        ),
        "LLM_REASONING_MODEL": "deepseek/deepseek-v4-pro",
        "LLM_FAST_MODEL": "deepseek/deepseek-v4-flash",
        "DM_NARRATION_MODEL": "dashscope/qwen3.5-plus",
    }


class ModelRegistryTests(unittest.TestCase):
    """验证目录解析、客户端缓存和显式失败边界。"""

    def test_builds_all_models_and_resolves_default_and_override_roles(self):
        calls: list[dict[str, str]] = []

        def factory(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(**kwargs)

        registry = build_model_registry(_environment(), model_factory=factory)

        self.assertEqual(len(registry.clients), 3)
        self.assertIs(
            registry.get_model("deepseek/deepseek-v4-pro"),
            registry.get_model("deepseek/deepseek-v4-pro"),
        )
        self.assertEqual(
            registry.model_name_for(ModelRole.DM_DECISION),
            "deepseek/deepseek-v4-pro",
        )
        self.assertEqual(
            registry.model_name_for(ModelRole.COMBAT_DECISION),
            "deepseek/deepseek-v4-flash",
        )
        self.assertEqual(
            registry.model_name_for(ModelRole.DM_NARRATION),
            "dashscope/qwen3.5-plus",
        )
        self.assertEqual(
            calls,
            [
                {
                    "model": "deepseek-v4-pro",
                    "base_url": "https://api.deepseek.com",
                    "api_key": "deepseek-secret",
                },
                {
                    "model": "deepseek-v4-flash",
                    "base_url": "https://api.deepseek.com",
                    "api_key": "deepseek-secret",
                },
                {
                    "model": "qwen3.5-plus",
                    "base_url": ("https://dashscope.aliyuncs.com/compatible-mode/v1"),
                    "api_key": "dashscope-secret",
                },
            ],
        )

    def test_rejects_invalid_catalog_and_role_references(self):
        cases: list[tuple[str, dict[str, str]]] = []

        missing_key = _environment()
        missing_key.pop("LLM_PROVIDER_DEEPSEEK_API_KEY")
        cases.append(("缺少供应商密钥", missing_key))

        bad_url = _environment()
        bad_url["LLM_PROVIDER_DEEPSEEK_BASE_URL"] = "deepseek.local"
        cases.append(("非法 URL", bad_url))

        malformed_model = _environment()
        malformed_model["LLM_MODELS"] = "deepseek-v4-pro"
        cases.append(("模型缺少供应商", malformed_model))

        unknown_provider = _environment()
        unknown_provider["LLM_MODELS"] += ",openrouter/openai/gpt"
        cases.append(("模型引用未知供应商", unknown_provider))

        unknown_reasoning = _environment()
        unknown_reasoning["LLM_REASONING_MODEL"] = "deepseek/missing"
        cases.append(("推理层引用未知模型", unknown_reasoning))

        unknown_override = _environment()
        unknown_override["DM_TRIGGER_MODEL"] = "dashscope/missing"
        cases.append(("职责覆盖引用未知模型", unknown_override))

        prefix_collision = _environment()
        prefix_collision["LLM_PROVIDERS"] = "a-b,a_b"
        prefix_collision["LLM_PROVIDER_A_B_BASE_URL"] = "https://example.com"
        prefix_collision["LLM_PROVIDER_A_B_API_KEY"] = "secret"
        cases.append(("供应商环境变量前缀冲突", prefix_collision))

        for label, environ in cases:
            with self.subTest(label=label):
                with self.assertRaises(LLMConfigurationError):
                    build_model_registry(
                        environ,
                        model_factory=lambda **_kwargs: object(),
                    )

    def test_unknown_model_and_role_fail_explicitly(self):
        registry = build_model_registry(
            _environment(),
            model_factory=lambda **_kwargs: object(),
        )

        with self.assertRaises(LLMConfigurationError):
            registry.get_model("deepseek/missing")
        with self.assertRaises(LLMConfigurationError):
            registry.model_name_for("missing_role")

    def test_model_initialization_error_does_not_expose_secret(self):
        def broken_factory(**_kwargs):
            raise ValueError("provider echoed deepseek-secret")

        with self.assertRaises(LLMConfigurationError) as raised:
            build_model_registry(_environment(), model_factory=broken_factory)

        self.assertNotIn("deepseek-secret", str(raised.exception))
        self.assertIn("ValueError", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    def test_global_initialization_is_idempotent(self):
        clients: list[object] = []

        def factory(**_kwargs):
            client = object()
            clients.append(client)
            return client

        llm_util._reset_model_registry_for_tests()
        try:
            with (
                patch.dict(os.environ, _environment(), clear=True),
                patch("src.common.utils.llm_util.load_dotenv"),
                patch("src.common.utils.llm_util.ChatOpenAI", side_effect=factory),
            ):
                first = llm_util.initialize_model_registry()
                second = llm_util.initialize_model_registry()

            self.assertIs(first, second)
            self.assertEqual(len(clients), 3)
        finally:
            llm_util._reset_model_registry_for_tests()


class ModelRegistryLifespanTests(unittest.TestCase):
    """验证 ASGI lifespan 在服务请求前初始化模型目录。"""

    def test_fastapi_lifespan_initializes_registry_without_model_probe(self):
        with patch("src.app.initialize_model_registry") as initialize:
            with TestClient(app) as client:
                response = client.get("/api/stories")

        self.assertEqual(response.status_code, 200)
        initialize.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
