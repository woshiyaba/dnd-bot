"""匿名多人房间接口的无模型合约测试。"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from src.app import app
from src.services.room_service import room_service


class RoomApiTests(unittest.TestCase):
    """验证创建、加入、角色占用和房主权限边界。"""

    def setUp(self):
        room_service.reset()
        self.client = TestClient(app)

    def test_create_and_join_room(self):
        created = self.client.post(
            "/api/rooms",
            json={"display_name": "甲", "character_id": "pc_aldous"},
        )
        self.assertEqual(created.status_code, 201)
        body = created.json()
        room_code = body["room"]["room_code"]
        self.assertEqual(len(room_code), 6)
        self.assertTrue(body["access_token"])
        self.assertTrue(body["member"]["is_host"])

        joined = self.client.post(
            f"/api/rooms/{room_code}/join",
            json={"display_name": "乙", "character_id": "pc_lyra"},
        )
        self.assertEqual(joined.status_code, 200)
        self.assertEqual(len(joined.json()["room"]["members"]), 2)

    def test_duplicate_character_and_name_are_rejected(self):
        created = self.client.post(
            "/api/rooms",
            json={"display_name": "甲", "character_id": "pc_aldous"},
        ).json()
        room_code = created["room"]["room_code"]

        duplicate_character = self.client.post(
            f"/api/rooms/{room_code}/join",
            json={"display_name": "乙", "character_id": "pc_aldous"},
        )
        self.assertEqual(duplicate_character.status_code, 409)

        duplicate_name = self.client.post(
            f"/api/rooms/{room_code}/join",
            json={"display_name": "甲", "character_id": "pc_lyra"},
        )
        self.assertEqual(duplicate_name.status_code, 409)

    def test_room_session_requires_bearer_token(self):
        created = self.client.post(
            "/api/rooms",
            json={"display_name": "甲", "character_id": "pc_aldous"},
        ).json()
        response = self.client.get(f"/api/rooms/{created['room']['room_code']}")
        self.assertEqual(response.status_code, 401)

    def test_character_catalog_matches_six_player_limit(self):
        response = self.client.get("/api/rooms/characters")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 6)


if __name__ == "__main__":
    unittest.main()
