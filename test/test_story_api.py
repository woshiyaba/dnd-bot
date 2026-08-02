"""故事广场与 Canon 发布链路的无模型合约测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.app import app
from src.common.utils.llm_util import ModelRole
from src.model.canon import Canon, validate_authored_canon, validate_canon
from src.schemas.story import StoryGenerationTaskResponse
from src.services.story_service import StoryService
import src.story.generator as story_generator
from src.story.loader import CanonRegistry
from src.story.generator import StoryGenerationError, _canon_errors, continue_interview

_CONFIRMED_BRIEF = {
    "revision": 1,
    "confirmed_revision": 1,
    "premise": "调查消失的星光",
    "player_role": "受邀进入天文塔的冒险者",
    "core_conflict": "阻止吞噬星光的仪式",
    "antagonist_direction": "system_design_secret",
    "gameplay_focus": ["调查", "社交", "战斗"],
    "tone": "神秘而明亮",
    "content_boundaries": [],
    "duration_minutes": 20,
    "player_count": 2,
    "ending_direction": "多结局",
    "user_confirmed": True,
}


def _generated_canon() -> tuple[dict, Canon]:
    raw = json.loads(Path("canon/whispers_bell_tower.json").read_text(encoding="utf-8"))
    raw.update(
        {
            "campaign_id": "test_starlight_archive",
            "title": "失落星图",
            "premise": "冒险者调查一座失去星光的天文塔。",
            "theme": "求知与代价",
            "tone": "神秘而明亮",
            "recommended_player_count": 2,
            "gameplay_focus": ["调查", "社交", "战斗"],
            "content_warnings": [],
        }
    )
    return raw, Canon.from_dict(raw)


class StoryApiTests(unittest.TestCase):
    """验证广场接口只返回公开摘要。"""

    def test_story_square_hides_canon_secrets(self):
        response = TestClient(app).get("/api/stories")
        self.assertEqual(response.status_code, 200, response.text)
        story = next(
            item
            for item in response.json()
            if item["campaign_id"] == "whispers_bell_tower"
        )
        self.assertEqual(story["duration_minutes"], 20)
        self.assertIn("gameplay_focus", story)
        self.assertNotIn("cast", story)
        self.assertNotIn("beats", story)
        self.assertNotIn("secret", json.dumps(story, ensure_ascii=False))

    def test_generation_task_endpoints_return_pollable_public_contract(self):
        now = datetime.now(UTC)
        queued = StoryGenerationTaskResponse(
            task_id="task_public_contract",
            status="queued",
            stage="等待生成",
            progress=0,
            created_at=now,
            updated_at=now,
        )
        cancelled = queued.model_copy(update={"status": "cancelled", "stage": "已取消"})
        client = TestClient(app)
        with patch(
            "src.api.stories.story_service.create_generation_task",
            new=AsyncMock(return_value=queued),
        ):
            created = client.post(
                "/api/stories/generation-tasks",
                json={"design_brief": _CONFIRMED_BRIEF},
            )
        with patch(
            "src.api.stories.story_service.get_generation_task",
            return_value=queued,
        ):
            fetched = client.get("/api/stories/generation-tasks/task_public_contract")
        with patch(
            "src.api.stories.story_service.cancel_generation_task",
            return_value=cancelled,
        ):
            deleted = client.delete(
                "/api/stories/generation-tasks/task_public_contract"
            )

        self.assertEqual(created.status_code, 202, created.text)
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(created.json()["task_id"], "task_public_contract")
        self.assertEqual(deleted.json()["status"], "cancelled")
        self.assertNotIn("design_brief", created.json())
        self.assertNotIn("story_plan", created.json())

    def test_duplicate_beat_ids_fail_canon_validation(self):
        raw, _canon = _generated_canon()
        raw["beats"].append(dict(raw["beats"][0]))

        errors = validate_canon(Canon.from_dict(raw))

        self.assertTrue(any("beat id" in error and "重复" in error for error in errors))

    def test_authored_canon_rejects_duplicate_flag_and_item_owners(self):
        raw, _canon = _generated_canon()
        ruined = next(beat for beat in raw["beats"] if beat["id"] == "ruined_village")
        summit = next(
            beat for beat in raw["beats"] if beat["id"] == "bell_tower_summit"
        )
        summit["encounter"]["on_win_flags"].append("clue_holy_water")
        duplicate_item_clue = next(
            clue for clue in ruined["key_info"] if clue["id"] == "clue_spirit_name"
        )
        duplicate_item_clue["discovery_effects"]["grant_items"] = [
            {
                "item_id": "item_holy_water",
                "quantity": 1,
                "recipient": "active_actor",
            }
        ]

        errors = validate_authored_canon(Canon.from_dict(raw))

        self.assertTrue(any("flag «clue_holy_water»" in error for error in errors))
        self.assertTrue(any("物品 «item_holy_water»" in error for error in errors))

        _parsed, generation_errors = _canon_errors(raw)
        self.assertTrue(
            any("flag «clue_holy_water»" in error for error in generation_errors)
        )

    def test_bundled_canon_has_single_atomic_item_and_flag_owners(self):
        path = Path("canon/prodigal_return_quest.json")
        canon = Canon.from_dict(json.loads(path.read_text(encoding="utf-8")))

        self.assertEqual(validate_canon(canon), [])
        self.assertEqual(validate_authored_canon(canon), [])

    def test_on_win_discoveries_must_reference_unique_clues_in_same_beat(self):
        raw, _canon = _generated_canon()
        ruined = next(beat for beat in raw["beats"] if beat["id"] == "ruined_village")
        summit = next(
            beat for beat in raw["beats"] if beat["id"] == "bell_tower_summit"
        )
        local_clue_id = ruined["key_info"][0]["id"]
        summit_clue_id = summit["key_info"][0]["id"]

        ruined["encounter"] = {
            "id": "ruined_village_encounter",
            "monster_ids": ["priest_eda"],
            "on_win_discoveries": [local_clue_id],
        }
        self.assertEqual(validate_canon(Canon.from_dict(raw)), [])

        ruined["encounter"]["on_win_discoveries"] = [
            summit_clue_id,
            "clue_missing",
            local_clue_id,
            local_clue_id,
        ]
        errors = validate_canon(Canon.from_dict(raw))

        self.assertTrue(any("属于另一拍" in error for error in errors))
        self.assertTrue(any("不存在的线索" in error for error in errors))
        self.assertTrue(any("重复引用线索" in error for error in errors))


class StoryGeneratorTests(unittest.IsolatedAsyncioTestCase):
    """确认模型输出损坏时显式失败，不创建假故事。"""

    def test_both_live_canons_are_loaded_as_generation_references(self):
        references = story_generator._load_reference_canons()

        self.assertEqual(
            [reference["campaign_id"] for reference in references],
            ["prodigal_return_quest", "whispers_bell_tower"],
        )
        prodigal_exploration = next(
            beat for beat in references[0]["beats"] if beat["id"] == "gate_exploration"
        )
        self.assertEqual(
            prodigal_exploration["encounter"]["on_win_discoveries"],
            ["clue_corpse_note"],
        )

    def test_generation_reloads_reference_canons_from_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            first_path = Path(directory) / "first.json"
            second_path = Path(directory) / "second.json"
            first_path.write_text(
                json.dumps({"campaign_id": "first", "revision": 1}),
                encoding="utf-8",
            )
            second_path.write_text(
                json.dumps({"campaign_id": "second"}),
                encoding="utf-8",
            )

            with patch.object(
                story_generator,
                "REFERENCE_CANON_PATHS",
                (first_path, second_path),
            ):
                initial = story_generator._load_reference_canons()
                first_path.write_text(
                    json.dumps({"campaign_id": "first", "revision": 2}),
                    encoding="utf-8",
                )
                reloaded = story_generator._load_reference_canons()

        self.assertEqual(initial[0]["revision"], 1)
        self.assertEqual(reloaded[0]["revision"], 2)

    async def test_story_interview_uses_dedicated_role_model(self):
        model = AsyncMock()
        model.ainvoke.return_value = SimpleNamespace(
            content=json.dumps(
                {
                    "status": "ready_for_confirmation",
                    "assistant_message": "请确认这份故事方向。",
                    "design_brief": {"user_confirmed": False},
                    "questions": [],
                },
                ensure_ascii=False,
            )
        )
        with (
            patch(
                "src.story.generator.get_model_name",
                return_value="deepseek/deepseek-v4-flash",
            ) as get_name,
            patch(
                "src.story.generator.get_chat_model",
                return_value=model,
            ) as get_model,
        ):
            await continue_interview(
                conversation=[{"role": "user", "content": "我想调查幽灵船"}],
                design_brief={},
            )

        get_name.assert_called_once_with(ModelRole.STORY_INTERVIEW)
        get_model.assert_called_once_with("deepseek/deepseek-v4-flash")

    async def test_story_interview_repairs_schema_errors_with_repair_role(self):
        invalid = {
            "status": "ready_for_confirmation",
            "assistant_message": "请确认这份故事方向。",
            "design_brief": {
                "branching_budget": {
                    "meaningful_branch_points": 0,
                    "max_parallel_beats": 1,
                    "reconverge_before_climax": True,
                    "choice_scope": "soft_choices_only",
                },
                "side_content": {
                    "desired_side_threads": 1,
                    "must_resolve_before_ending": True,
                    "focus": "恩人或旧友委托，不牵动主线因果",
                },
                "user_confirmed": False,
            },
            "questions": [],
        }
        repaired = {
            "status": "ready_for_confirmation",
            "assistant_message": "请确认这份故事方向。",
            "design_brief": {
                "branching_budget": {
                    "meaningful_branch_points": 0,
                    "max_parallel_beats": 1,
                    "reconverge_before_climax": True,
                },
                "side_content": {
                    "desired_side_threads": 1,
                    "must_resolve_before_ending": True,
                },
                "must_have": ["恩人或旧友委托，不牵动主线因果"],
                "user_confirmed": False,
            },
            "questions": [],
        }
        completion = AsyncMock(side_effect=[invalid, repaired])

        with patch("src.story.generator._complete_json", completion):
            response = await continue_interview(
                conversation=[{"role": "user", "content": "保留旧友委托支线"}],
                design_brief={"tone": "悬疑"},
            )

        self.assertEqual(
            response.design_brief.must_have, ["恩人或旧友委托，不牵动主线因果"]
        )
        self.assertEqual(
            [call.kwargs["role"] for call in completion.await_args_list],
            [ModelRole.STORY_INTERVIEW, ModelRole.STORY_REPAIR],
        )
        repair_prompt = completion.await_args_list[1].args[0]
        self.assertIn("design_brief.branching_budget.choice_scope", repair_prompt)
        self.assertIn("design_brief.side_content.focus", repair_prompt)
        self.assertIn("soft_choices_only", repair_prompt)
        self.assertIn("StoryInterviewResponse", repair_prompt)

    async def test_story_interview_repairs_all_long_scale_conflicts_at_once(self):
        invalid = {
            "status": "confirmed",
            "assistant_message": "设计已确认。",
            "design_brief": {
                "duration_minutes": 90,
                "length_mode": "long",
                "scale_profile": {
                    "playable_beats": 5,
                    "acts": 3,
                    "locations": 5,
                    "encounters": 3,
                    "clues": 3,
                },
                "branching_style": "branch_and_reconverge",
                "branching_budget": {
                    "meaningful_branch_points": 2,
                    "max_parallel_beats": 1,
                    "reconverge_before_climax": True,
                },
                "user_confirmed": True,
            },
            "questions": [],
        }
        repaired = {
            **invalid,
            "design_brief": {
                **invalid["design_brief"],
                "scale_profile": {
                    "playable_beats": 8,
                    "acts": 4,
                    "locations": 8,
                    "encounters": 3,
                    "clues": 7,
                },
            },
        }
        completion = AsyncMock(side_effect=[invalid, repaired])

        with patch("src.story.generator._complete_json", completion):
            response = await continue_interview(
                conversation=[{"role": "user", "content": "确认按这个生成"}],
                design_brief=invalid["design_brief"],
            )

        self.assertEqual(response.design_brief.scale_profile.playable_beats, 8)
        self.assertEqual(completion.await_count, 2)
        repair_prompt = completion.await_args_list[1].args[0]
        for field in ("playable_beats", "acts", "locations", "clues"):
            self.assertIn(f"scale_profile.{field}", repair_prompt)

    async def test_story_interview_fails_after_two_invalid_repairs(self):
        invalid = {
            "status": "ready_for_confirmation",
            "assistant_message": "请确认。",
            "design_brief": {
                "branching_budget": {"choice_scope": "soft_choices_only"},
                "user_confirmed": False,
            },
            "questions": [],
        }
        completion = AsyncMock(return_value=invalid)

        with patch("src.story.generator._complete_json", completion):
            with self.assertRaisesRegex(
                StoryGenerationError,
                "design_brief.branching_budget.choice_scope",
            ):
                await continue_interview(
                    conversation=[{"role": "user", "content": "我想调查幽灵船"}],
                    design_brief={},
                )

        self.assertEqual(completion.await_count, 3)
        self.assertEqual(
            [call.kwargs["role"] for call in completion.await_args_list],
            [
                ModelRole.STORY_INTERVIEW,
                ModelRole.STORY_REPAIR,
                ModelRole.STORY_REPAIR,
            ],
        )

    async def test_invalid_llm_json_fails_explicitly(self):
        model = AsyncMock()
        model.ainvoke.return_value = SimpleNamespace(content="这不是 JSON")
        with (
            patch(
                "src.story.generator.get_model_name",
                return_value="deepseek/deepseek-v4-flash",
            ),
            patch("src.story.generator.get_chat_model", return_value=model),
        ):
            with self.assertRaises(StoryGenerationError):
                await continue_interview(
                    conversation=[{"role": "user", "content": "我想调查幽灵船"}],
                    design_brief={},
                )
        model.ainvoke.assert_awaited_once()

    async def test_canon_authoring_and_repair_use_reasoning_roles(self):
        valid_raw, _canon = _generated_canon()
        completion = AsyncMock(
            side_effect=[
                {"campaign_id": "broken"},
                valid_raw,
            ]
        )
        with patch("src.story.generator._complete_json", completion):
            generated_raw, _generated = await story_generator.generate_canon(
                confirmed_brief=_CONFIRMED_BRIEF
            )

        self.assertEqual(generated_raw["campaign_id"], "test_starlight_archive")
        self.assertEqual(
            [call.kwargs["role"] for call in completion.await_args_list],
            [ModelRole.STORY_AUTHORING, ModelRole.STORY_REPAIR],
        )


class StoryPublishingTests(unittest.IsolatedAsyncioTestCase):
    """验证预览后发布、即时注册与防覆盖边界。"""

    async def test_validated_draft_is_written_and_registered(self):
        raw, canon = _generated_canon()
        registry = CanonRegistry()
        with tempfile.TemporaryDirectory() as directory:
            service = StoryService(Path(directory))
            with (
                patch(
                    "src.services.story_service.generate_canon",
                    new=AsyncMock(return_value=(raw, canon)),
                ),
                patch("src.services.story_service.get_registry", return_value=registry),
            ):
                draft = await service.create_draft(_CONFIRMED_BRIEF)
                summary = await service.publish(draft.draft_id)

            target = Path(directory) / "test_starlight_archive.json"
            self.assertTrue(target.is_file())
            self.assertEqual(summary.title, "失落星图")
            self.assertIs(registry.get("test_starlight_archive"), canon)

    async def test_publish_never_overwrites_existing_campaign(self):
        raw, canon = _generated_canon()
        registry = CanonRegistry()
        registry.register(canon)
        with tempfile.TemporaryDirectory() as directory:
            service = StoryService(Path(directory))
            with (
                patch(
                    "src.services.story_service.generate_canon",
                    new=AsyncMock(return_value=(raw, canon)),
                ),
                patch("src.services.story_service.get_registry", return_value=registry),
            ):
                draft = await service.create_draft(_CONFIRMED_BRIEF)
                with self.assertRaises(HTTPException) as raised:
                    await service.publish(draft.draft_id)

            self.assertEqual(raised.exception.status_code, 409)

    async def test_publish_rejects_duplicate_atomic_owners(self):
        raw, _canon = _generated_canon()
        summit = next(
            beat for beat in raw["beats"] if beat["id"] == "bell_tower_summit"
        )
        summit["encounter"]["on_win_flags"].append("clue_holy_water")
        canon = Canon.from_dict(raw)
        registry = CanonRegistry()
        with tempfile.TemporaryDirectory() as directory:
            service = StoryService(Path(directory))
            with (
                patch(
                    "src.services.story_service.generate_canon",
                    new=AsyncMock(return_value=(raw, canon)),
                ),
                patch("src.services.story_service.get_registry", return_value=registry),
            ):
                draft = await service.create_draft(_CONFIRMED_BRIEF)
                with self.assertRaises(HTTPException) as raised:
                    await service.publish(draft.draft_id)

        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("多个原子写入入口", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()
