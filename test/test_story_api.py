"""故事广场与 Canon 发布链路的无模型合约测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.app import app
from src.model.canon import Canon, validate_authored_canon, validate_canon
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

    async def test_story_generation_uses_dedicated_model(self):
        previous_model = story_generator._model
        story_generator._model = None
        model = object()
        try:
            with patch(
                "src.story.generator.create_chat_model",
                return_value=model,
            ) as create_model:
                resolved = await story_generator._get_model()

            self.assertIs(resolved, model)
            self.assertEqual(
                story_generator.DEFAULT_STORY_GENERATION_MODEL,
                "deepseek-v4-pro",
            )
            create_model.assert_called_once_with(
                model=story_generator.STORY_GENERATION_MODEL,
                enable_search=False,
            )
        finally:
            story_generator._model = previous_model

    async def test_invalid_llm_json_fails_explicitly(self):
        model = AsyncMock()
        model.ainvoke.return_value = SimpleNamespace(content="这不是 JSON")
        with patch(
            "src.story.generator._get_model",
            new=AsyncMock(return_value=model),
        ):
            with self.assertRaises(StoryGenerationError):
                await continue_interview(
                    conversation=[{"role": "user", "content": "我想调查幽灵船"}],
                    design_brief={},
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
