"""匿名多人房间与玩家自建角色接口的无模型合约测试。"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from src.app import app
from src.services.room_service import room_service


def _character(class_id: str = "fighter", race_id: str = "human") -> dict:
    return {
        "race_id": race_id,
        "class_id": class_id,
        "base_abilities": {
            "strength": 15,
            "dexterity": 14,
            "constitution": 15,
            "intelligence": 8,
            "wisdom": 10,
            "charisma": 8,
        },
        "racial_bonus_choices": [],
    }


class RoomApiTests(unittest.TestCase):
    """验证创建、加入、自建角色和房主身份边界。"""

    def setUp(self):
        room_service.reset()
        self.client = TestClient(app)

    def test_create_and_join_room_with_same_class(self):
        created = self.client.post(
            "/api/rooms",
            json={"display_name": "甲", "character": _character()},
        )
        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        room_code = body["room"]["room_code"]
        self.assertEqual(len(room_code), 6)
        self.assertTrue(body["access_token"])
        self.assertTrue(body["member"]["is_host"])
        self.assertEqual(body["member"]["character"]["char_class"], "战士")

        joined = self.client.post(
            f"/api/rooms/{room_code}/join",
            json={"display_name": "乙", "character": _character()},
        )
        self.assertEqual(joined.status_code, 200, joined.text)
        members = joined.json()["room"]["members"]
        self.assertEqual(len(members), 2)
        self.assertNotEqual(members[0]["character_id"], members[1]["character_id"])

    def test_duplicate_name_is_rejected_but_class_is_not_reserved(self):
        created = self.client.post(
            "/api/rooms",
            json={"display_name": "甲", "character": _character()},
        ).json()
        room_code = created["room"]["room_code"]
        duplicate_name = self.client.post(
            f"/api/rooms/{room_code}/join",
            json={"display_name": "甲", "character": _character("paladin")},
        )
        self.assertEqual(duplicate_name.status_code, 409)

    def test_invalid_point_buy_is_rejected(self):
        character = _character()
        character["base_abilities"]["wisdom"] = 9
        response = self.client.post(
            "/api/rooms",
            json={"display_name": "甲", "character": character},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("27", response.json()["detail"])

    def test_room_session_requires_bearer_token(self):
        created = self.client.post(
            "/api/rooms",
            json={"display_name": "甲", "character": _character()},
        ).json()
        response = self.client.get(f"/api/rooms/{created['room']['room_code']}")
        self.assertEqual(response.status_code, 401)

    def test_unknown_campaign_is_rejected(self):
        response = self.client.post(
            "/api/rooms",
            json={
                "display_name": "甲",
                "character": _character(),
                "campaign_id": "missing_campaign",
            },
        )
        self.assertEqual(response.status_code, 404)
        self.assertIn("剧本不存在", response.json()["detail"])

    def test_character_creation_catalog_has_six_races_and_five_classes(self):
        response = self.client.get("/api/rooms/character-options")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["races"]), 6)
        self.assertEqual(len(body["classes"]), 5)
        self.assertEqual(body["point_buy"]["budget"], 27)


if __name__ == "__main__":
    unittest.main()
