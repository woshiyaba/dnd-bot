"""Pro/Flash 职责路由与无工具叙述边界的离线测试。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.combat import action_compiler, dm_bridge
from src.common.utils.llm_util import ModelRole
from src.dm import agent as dm_agent
from src.dm import world_bridge


class DMAgentAssemblyTests(unittest.IsolatedAsyncioTestCase):
    """验证决策 Agent 缓存包含模型，叙述不会创建工具 Agent。"""

    def setUp(self):
        dm_agent._cached_agents.clear()

    def tearDown(self):
        dm_agent._cached_agents.clear()

    async def test_decision_agents_are_cached_per_model(self):
        pro_agent = object()
        flash_agent = object()
        with (
            patch("src.dm.agent.build_dm_system_prompt", return_value="system"),
            patch(
                "src.dm.agent.get_chat_model", side_effect=["pro-model", "flash-model"]
            ),
            patch(
                "src.dm.agent.create_agent",
                side_effect=[pro_agent, flash_agent],
            ) as create,
        ):
            first = await dm_agent.get_dm_agent("deepseek/deepseek-v4-pro")
            repeated = await dm_agent.get_dm_agent("deepseek/deepseek-v4-pro")
            second = await dm_agent.get_dm_agent("deepseek/deepseek-v4-flash")

        self.assertIs(first, pro_agent)
        self.assertIs(repeated, pro_agent)
        self.assertIs(second, flash_agent)
        self.assertEqual(create.call_count, 2)
        self.assertEqual(create.call_args_list[0].args[0], "pro-model")
        self.assertEqual(create.call_args_list[1].args[0], "flash-model")
        self.assertTrue(create.call_args_list[0].kwargs["tools"])

    async def test_narration_streams_directly_without_creating_agent(self):
        class FakeModel:
            def __init__(self):
                self.messages = None

            async def astream(self, messages):
                self.messages = messages
                for text in ("钟声", "回荡"):
                    yield SimpleNamespace(content=text)

        model = FakeModel()
        with (
            patch("src.dm.agent.build_dm_system_prompt", return_value="DM system"),
            patch("src.dm.agent.get_chat_model", return_value=model),
            patch("src.dm.agent.create_agent") as create,
        ):
            result = await dm_agent.dm_narrate(
                "描述场景",
                model_name="deepseek/deepseek-v4-flash",
                node_name=None,
            )

        self.assertEqual(result, "钟声回荡")
        self.assertEqual(
            model.messages,
            [
                {"role": "system", "content": "DM system"},
                {"role": "user", "content": "描述场景"},
            ],
        )
        create.assert_not_called()


class RoleRoutingTests(unittest.IsolatedAsyncioTestCase):
    """验证各业务边界把正确职责交给中央模型目录。"""

    async def test_world_decision_trigger_and_narration_use_separate_roles(self):
        complete = AsyncMock(
            side_effect=[
                {"intent": "reply", "reply_brief": "回应玩家。"},
                {"answer": True, "reason": "玩家已经行动。"},
            ]
        )
        narrate = AsyncMock(return_value="镜头转向前方。")

        def model_name(role):
            return f"selected/{role.value}"

        with (
            patch("src.dm.world_bridge.get_model_name", side_effect=model_name),
            patch("src.dm.world_bridge.dm_complete_json", complete),
            patch("src.dm.world_bridge.dm_narrate", narrate),
        ):
            await world_bridge._decide_llm(
                "我观察房间",
                {"location": "大厅", "actors": []},
                {},
                [],
            )
            answer = await world_bridge.judge_trigger(
                "玩家是否已经观察房间？",
                {"location": "大厅", "actors": []},
                user_input="我观察房间",
                messages=[],
                use_llm=True,
            )
            await world_bridge.narrate_beat_transition(
                "下一幕",
                {"location": "塔顶", "actors": []},
                use_llm=True,
            )

        self.assertTrue(answer)
        self.assertEqual(
            complete.await_args_list[0].kwargs["model_name"],
            "selected/dm_decision",
        )
        self.assertEqual(
            complete.await_args_list[1].kwargs["model_name"],
            "selected/dm_trigger",
        )
        self.assertEqual(
            narrate.await_args.kwargs["model_name"],
            "selected/dm_narration",
        )

    async def test_combat_decision_and_narration_use_flash_roles(self):
        complete = AsyncMock(return_value={"surprised": []})
        narrate = AsyncMock(return_value="战斗开始。")

        def model_name(role):
            return f"selected/{role.value}"

        with (
            patch("src.combat.dm_bridge.get_model_name", side_effect=model_name),
            patch("src.combat.dm_bridge.dm_complete_json", complete),
            patch("src.combat.dm_bridge.dm_narrate", narrate),
        ):
            self.assertEqual(await dm_bridge.judge_surprise_llm({}, {}), [])
            await dm_bridge.narrate_combat_opening_llm({}, {})

        self.assertEqual(
            complete.await_args.kwargs["model_name"],
            "selected/combat_decision",
        )
        self.assertEqual(
            narrate.await_args.kwargs["model_name"],
            "selected/combat_narration",
        )

    async def test_action_compiler_agent_uses_its_role_model(self):
        action_compiler._cached_agents.clear()
        agent = object()
        with (
            patch(
                "src.combat.action_compiler.get_model_name",
                return_value="deepseek/deepseek-v4-flash",
            ) as get_name,
            patch(
                "src.combat.action_compiler.get_chat_model",
                return_value="flash-model",
            ) as get_model,
            patch(
                "src.combat.action_compiler.create_agent",
                return_value=agent,
            ) as create,
        ):
            resolved = await action_compiler._get_agent()

        self.assertIs(resolved, agent)
        get_name.assert_called_once_with(ModelRole.ACTION_COMPILER)
        get_model.assert_called_once_with("deepseek/deepseek-v4-flash")
        self.assertEqual(create.call_args.kwargs["tools"], [])
        action_compiler._cached_agents.clear()


if __name__ == "__main__":
    unittest.main()
