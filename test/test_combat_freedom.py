"""战斗触发、自然语言行动与线索战斗效果的无模型回归测试。"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from src.dm import world_bridge
from src.combat.dm_bridge import validate_player_action
from src.combat.engine import CombatEngine
from src.combat.interrupts import build_action_options, build_combat_view
from src.combat.nodes import resolve_action
from src.model.attack import Attack
from src.model.canon import beat_brief
from src.model.combatant import Monster, PlayerCharacter
from src.model.dm_state import build_beat_scene
from src.model.effects import InventoryItem
from src.model.enums import ConditionType, DamageType, Faction, Range
from src.services.room_service import GameRoom, RoomMember
from src.services.session_service import session_service
from src.session import story_nodes
from src.session.dm_subgraph import build_dm_subgraph
from src.session.engine import SessionEngine
from src.session.graph import _build_combat_input, resolve_engagement
from src.story.loader import get_registry


class CombatFreedomTests(unittest.TestCase):
    """验证自由开战仍会在规则边界内解析成严格战斗输入。"""

    @classmethod
    def setUpClass(cls) -> None:
        registry = get_registry()
        registry.load_all()
        cls.canon = registry.get("whispers_bell_tower")

    def _player(self) -> PlayerCharacter:
        player = PlayerCharacter.from_card(
            {
                "id": "pc_aldous",
                "name": "奥尔德斯",
                "strength": 16,
                "charisma": 14,
                "current_hp": 20,
                "max_hp": 20,
                "ac": 16,
                "attacks": [
                    {
                        "name": "长剑",
                        "attack_bonus": 5,
                        "damage_dice": "1d8+3",
                        "damage_type": "slashing",
                        "range": "melee",
                    }
                ],
            }
        )
        player.controller = "user_aldous"
        return player

    def test_composite_attack_transitions_and_resolves_boss_encounter(self):
        ruined = self.canon.beat("ruined_village")
        scene = build_beat_scene(self.canon, ruined)
        player = self._player()
        state = {
            "campaign_id": self.canon.campaign_id,
            "story": {
                "current_beat_id": ruined.id,
                "current_location_id": scene["location_id"],
                "visited_beats": [ruined.id],
                "visited_locations": [scene["location_id"]],
                "flags": {},
                "delivered_clues": [],
                "discovered_clues": [],
                "removed_actor_ids": [],
                "turn_index": 0,
            },
            "scene": scene,
            "party": {player.id: player},
            "campaign_log": [],
            "combat_request": {
                "encounter_id": "boss_bell_spirit",
                "target_actor_ids": ["bell_spirit"],
                "reason": "玩家冲上钟楼直接攻击古钟之灵",
                "before_combat": {"transition_to_beat_id": "bell_tower_summit"},
            },
        }

        update = resolve_engagement(state)
        merged = {**state, **update}
        combatants, context = _build_combat_input(merged)

        self.assertEqual(update["story"]["current_beat_id"], "bell_tower_summit")
        self.assertEqual(update["scene"]["beat_id"], "bell_tower_summit")
        self.assertEqual(update["scene"]["location_id"], "bell_tower_summit")
        self.assertIn("bell_spirit", combatants)
        self.assertEqual(context["encounter_id"], "boss_bell_spirit")
        self.assertEqual(context["random_seed"], 20240626)
        self.assertEqual(update["story"].get("discovered_clues", []), [])

    def test_dm_context_exposes_reachable_encounter_and_closed_ids(self):
        ruined = self.canon.beat("ruined_village")
        brief = beat_brief(
            self.canon,
            {
                "current_beat_id": ruined.id,
                "flags": {"accepted_quest": True},
                "delivered_clues": [],
                "discovered_clues": [],
            },
        )

        self.assertIn("accepted_quest", brief["allowed_flags"])
        self.assertIn(
            "clue_holy_water",
            brief["allowed_discovery_clue_ids"],
        )
        holy_water = next(
            item
            for item in brief["available_discoveries"]
            if item["id"] == "clue_holy_water"
        )
        self.assertEqual(
            holy_water["discovery_effects"]["grant_items"][0]["item_id"],
            "item_holy_water",
        )
        self.assertTrue(brief["current_flags"]["accepted_quest"])
        self.assertEqual(
            brief["managed_flag_sources"]["boss_dead"][0]["kind"],
            "encounter_win",
        )
        self.assertEqual(
            brief["reachable_encounters"][0]["encounter_id"],
            "boss_bell_spirit",
        )

        player = self._player()
        player.inventory.append(InventoryItem(item_id="item_map", quantity=1))
        party_brief = world_bridge._party_brief({player.id: player})
        self.assertEqual(
            party_brief[0]["inventory"],
            [{"item_id": "item_map", "quantity": 1}],
        )

        discovered_brief = beat_brief(
            self.canon,
            {
                "current_beat_id": ruined.id,
                "flags": {"clue_holy_water": True},
                "delivered_clues": ["clue_holy_water"],
                "discovered_clues": ["clue_holy_water"],
            },
        )
        self.assertNotIn(
            "clue_holy_water",
            discovered_brief["allowed_discovery_clue_ids"],
        )
        self.assertNotIn(
            "clue_holy_water",
            {item["id"] for item in discovered_brief["available_discoveries"]},
        )

    def test_discovery_atomically_sets_flag_and_grants_item(self):
        ruined = self.canon.beat("ruined_village")
        scene = build_beat_scene(self.canon, ruined)
        player = self._player()
        story = {
            "current_beat_id": ruined.id,
            "current_location_id": scene["location_id"],
            "visited_locations": [scene["location_id"]],
            "flags": {},
            "delivered_clues": [],
            "discovered_clues": [],
        }
        state = {
            "scene": scene,
            "party": {player.id: player},
            "active_actor_id": player.id,
            "world_writes": {"discoveries": ["clue_holy_water"]},
        }

        updated_story, _, _, _ = story_nodes._apply_world_writes(
            self.canon,
            story,
            state,
        )

        self.assertTrue(updated_story["flags"]["clue_holy_water"])
        self.assertEqual(updated_story["discovered_clues"], ["clue_holy_water"])
        self.assertEqual(player.inventory[0].item_id, "item_holy_water")

        state["world_writes"] = {"flags_set": {"clue_spirit_name": True}}
        with self.assertRaises(ValueError):
            story_nodes._apply_world_writes(self.canon, story, state)

        summit = self.canon.beat("bell_tower_summit")
        summit_brief = beat_brief(
            self.canon,
            {
                "current_beat_id": summit.id,
                "flags": {},
                "delivered_clues": [],
                "discovered_clues": [],
            },
        )
        with self.assertRaises(world_bridge.WorldStateDecisionError):
            world_bridge._world_writes(
                {"flags_set": {"boss_dead": True}},
                summit_brief,
            )

    def test_legacy_win_flag_does_not_hide_undiscovered_key_source(self):
        legacy = get_registry().get("prodigal_return_quest")
        brief = beat_brief(
            legacy,
            {
                "current_beat_id": "gate_exploration",
                "flags": {"key_obtained": True},
                "delivered_clues": [],
                "discovered_clues": [],
            },
        )

        corpse_note = next(
            clue
            for clue in brief["available_discoveries"]
            if clue["id"] == "clue_corpse_note"
        )
        self.assertEqual(
            corpse_note["discovery_effects"]["grant_items"][0]["item_id"],
            "item_copper_key",
        )
        self.assertEqual(
            world_bridge._world_writes(
                {
                    "discoveries": ["clue_corpse_note"],
                    "moved_to": "inscription_hall",
                },
                brief,
            ),
            {
                "discoveries": ["clue_corpse_note"],
                "moved_to": "inscription_hall",
            },
        )

        player = self._player()
        story = {
            "current_beat_id": "gate_exploration",
            "current_location_id": "ruined_gate",
            "visited_locations": ["ruined_gate"],
            "flags": {"key_obtained": True},
            "delivered_clues": [],
            "discovered_clues": [],
        }
        updated_story, _scene, _party, _events = story_nodes._apply_world_writes(
            legacy,
            story,
            {
                "scene": build_beat_scene(
                    legacy,
                    legacy.beat("gate_exploration"),
                ),
                "party": {player.id: player},
                "active_actor_id": player.id,
                "world_writes": {"discoveries": ["clue_corpse_note"]},
            },
        )
        self.assertIn("clue_corpse_note", updated_story["discovered_clues"])
        self.assertEqual(player.inventory[0].item_id, "item_copper_key")


class TransitionWriteValidationTests(unittest.IsolatedAsyncioTestCase):
    """验证 DM 只能显式提交 action 类型的跨拍行动。"""

    @classmethod
    def setUpClass(cls) -> None:
        registry = get_registry()
        registry.load_all()
        cls.canon = registry.get("whispers_bell_tower")

    async def test_semantic_transition_is_rejected_and_retried(self):
        tavern = self.canon.beat("tavern_quest")
        brief = beat_brief(
            self.canon,
            {
                "current_beat_id": tavern.id,
                "delivered_clues": [],
            },
        )
        decisions = AsyncMock(
            side_effect=[
                {
                    "intent": "reply",
                    "reply_brief": "确认玩家接受委托并准备出发。",
                    "flags_set": {"accepted_quest": True},
                    "transition_to_beat_id": "ruined_village",
                },
                {
                    "intent": "reply",
                    "reply_brief": "确认玩家接受委托并准备出发。",
                    "flags_set": {"accepted_quest": True},
                },
            ]
        )

        with patch("src.dm.world_bridge._decide_llm", decisions):
            result = await world_bridge.decide_turn(
                "我接受委托，现在就出发去废村。",
                {"location": "破钟酒馆", "actors": []},
                {},
                beat_brief=brief,
            )
        self.assertEqual(decisions.await_count, 2)
        self.assertEqual(
            result["world_writes"],
            {"flags_set": {"accepted_quest": True}},
        )

        scene = build_beat_scene(self.canon, tavern)
        state = {
            "campaign_id": self.canon.campaign_id,
            "story": {
                "current_beat_id": tavern.id,
                "current_location_id": scene["location_id"],
                "visited_beats": [tavern.id],
                "visited_locations": [scene["location_id"]],
                "flags": {},
                "delivered_clues": [],
                "discovered_clues": [],
            },
            "scene": scene,
            "party": {},
            "campaign_log": [],
            "user_input": "我接受委托，现在就出发去废村。",
            "messages": [],
            "world_writes": result["world_writes"],
        }
        with patch(
            "src.dm.world_bridge.judge_trigger",
            new=AsyncMock(return_value=True),
        ):
            advancement = await story_nodes.evaluate_advancement(state)

        self.assertEqual(advancement["next_story"], "advance")
        self.assertEqual(
            advancement["story"]["pending_next_beat_id"],
            "ruined_village",
        )

    async def test_action_transition_is_accepted(self):
        ruined = self.canon.beat("ruined_village")
        brief = beat_brief(
            self.canon,
            {
                "current_beat_id": ruined.id,
                "delivered_clues": [],
            },
        )
        decisions = AsyncMock(
            return_value={
                "intent": "reply",
                "reply_brief": "确认玩家已登上钟楼之巅。",
                "transition_to_beat_id": "bell_tower_summit",
            }
        )

        with patch("src.dm.world_bridge._decide_llm", decisions):
            result = await world_bridge.decide_turn(
                "我登上钟楼之巅。",
                {"location": "废村·钟楼大厅", "actors": []},
                {},
                beat_brief=brief,
            )

        self.assertEqual(decisions.await_count, 1)
        self.assertEqual(
            result["world_writes"]["transition_to_beat_id"],
            "bell_tower_summit",
        )

    def test_semantic_transition_is_rejected_before_combat(self):
        context = {
            "beat_id": "tavern_quest",
            "reachable_transitions": [
                {
                    "trigger_kind": "semantic",
                    "to_beat_id": "ruined_village",
                }
            ],
            "reachable_encounters": [
                {
                    "encounter_id": "village_ambush",
                    "beat_id": "ruined_village",
                    "monster_ids": ["ambusher"],
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "合法 action 出口"):
            world_bridge._normalize_decision(
                {
                    "intent": "start_combat",
                    "encounter": {
                        "encounter_id": "village_ambush",
                        "target_actor_ids": ["ambusher"],
                    },
                    "before_combat": {
                        "transition_to_beat_id": "ruined_village",
                    },
                },
                {},
                [],
                decision_context=context,
            )


class WorldStateGuidanceTests(unittest.IsolatedAsyncioTestCase):
    """确认连续世界写入冲突会进入真实 LLM 引导，而不是令会话返回 500。"""

    @classmethod
    def setUpClass(cls) -> None:
        registry = get_registry()
        registry.load_all()
        cls.canon = registry.get("whispers_bell_tower")

    def _state(self) -> dict:
        ruined = self.canon.beat("ruined_village")
        scene = build_beat_scene(self.canon, ruined)
        player = CombatFreedomTests()._player()
        return {
            "campaign_id": self.canon.campaign_id,
            "story": {
                "current_beat_id": ruined.id,
                "current_location_id": scene["location_id"],
                "visited_beats": [ruined.id],
                "visited_locations": [scene["location_id"]],
                "flags": {"accepted_quest": True},
                "delivered_clues": [],
                "discovered_clues": [],
                "removed_actor_ids": [],
                "turn_index": 0,
                "idle_turns": 0,
            },
            "scene": scene,
            "party": {player.id: player},
            "active_actor_id": player.id,
            "active_user_id": player.controller,
            "messages": [],
            "campaign_log": [],
            "user_input": "我直接拿起圣水",
        }

    async def test_retry_hint_contains_discovery_effect_mapping(self):
        state = self._state()
        brief = beat_brief(self.canon, state["story"])
        decisions = AsyncMock(
            side_effect=[
                {
                    "intent": "reply",
                    "reply_brief": "拿起圣水。",
                    "discoveries": ["clue_holy_water"],
                },
                {
                    "intent": "reply",
                    "reply_brief": "再次拿起圣水。",
                    "discoveries": ["clue_holy_water"],
                },
                {
                    "intent": "reply",
                    "reply_brief": "仍然重复拿取。",
                    "discoveries": ["clue_holy_water"],
                },
            ]
        )
        brief["discovered_clue_ids"] = ["clue_holy_water"]
        brief["allowed_discovery_clue_ids"] = []
        brief["available_discoveries"] = []

        with patch("src.dm.world_bridge._decide_llm", decisions):
            with self.assertRaises(world_bridge.WorldStateDecisionExhausted):
                await world_bridge.decide_turn(
                    "我再次拿起圣水",
                    state["scene"],
                    state["party"],
                    beat_brief=brief,
                )

        correction = decisions.await_args_list[1].kwargs["correction_hint"]
        self.assertIn("合法 discoveries id 与原子效果", correction)
        self.assertIn("已经发现的线索 id", correction)
        self.assertIn("clue_holy_water", correction)

    async def test_dm_subgraph_routes_world_conflict_to_guidance(self):
        state = self._state()
        with (
            patch(
                "src.session.dm_subgraph.world_bridge.decide_turn",
                new=AsyncMock(
                    side_effect=world_bridge.WorldStateDecisionExhausted(
                        "[dm] discoveries 含非法线索"
                    )
                ),
            ),
            patch(
                "src.session.dm_subgraph.world_bridge.plan_world_state_guidance",
                new=AsyncMock(
                    return_value={
                        "reply_brief": "提示玩家去墓园调查圣水，或寻找其它可行路线。",
                        "narrative_intent": "让远处墓园的钟声成为方向提示。",
                    }
                ),
            ),
        ):
            result = await build_dm_subgraph().ainvoke(state)

        self.assertEqual(result["intent"], "reply")
        self.assertIn("墓园", result["reply_brief"])
        self.assertIsNone(result.get("world_writes"))
        self.assertEqual(result["story"]["flags"], {"accepted_quest": True})
        self.assertEqual(result["party"]["pc_aldous"].inventory, [])

    async def test_guidance_plan_rejects_world_writes_and_retries(self):
        completions = AsyncMock(
            side_effect=[
                {
                    "reply_brief": "直接把圣水给玩家。",
                    "discoveries": ["clue_holy_water"],
                },
                {
                    "reply_brief": "提示玩家调查墓园无名碑。",
                    "narrative_intent": "让潮湿石碑反射微光。",
                },
            ]
        )
        state = self._state()

        with patch("src.dm.world_bridge.dm_complete_json", completions):
            result = await world_bridge.plan_world_state_guidance(
                state["user_input"],
                state["scene"],
                state["party"],
                issue="非法 discovery",
                beat_brief=beat_brief(self.canon, state["story"]),
            )

        self.assertEqual(completions.await_count, 2)
        self.assertIn("墓园", result["reply_brief"])
        self.assertNotIn("discoveries", result)

    async def test_unparseable_decisions_remain_nonrecoverable(self):
        state = self._state()
        decisions = AsyncMock(return_value=None)

        with patch("src.dm.world_bridge._decide_llm", decisions):
            with self.assertRaises(RuntimeError) as raised:
                await world_bridge.decide_turn(
                    state["user_input"],
                    state["scene"],
                    state["party"],
                    beat_brief=beat_brief(self.canon, state["story"]),
                )

        self.assertNotIsInstance(
            raised.exception,
            world_bridge.WorldStateDecisionExhausted,
        )

    async def test_session_engine_finishes_guidance_turn_without_error(self):
        player = CombatFreedomTests()._player()
        scene_context = {
            "campaign_id": self.canon.campaign_id,
            "active_actor_id": player.id,
            "active_user_id": player.controller,
            "party": [
                {
                    "type": "player",
                    "controller": player.controller,
                    "card": player.to_card(),
                }
            ],
        }
        with (
            patch(
                "src.session.dm_subgraph.world_bridge.decide_turn",
                new=AsyncMock(
                    side_effect=world_bridge.WorldStateDecisionExhausted(
                        "[dm] discoveries 含非法线索"
                    )
                ),
            ),
            patch(
                "src.session.dm_subgraph.world_bridge.plan_world_state_guidance",
                new=AsyncMock(
                    return_value={
                        "reply_brief": "提示玩家先调查酒馆里的委托线索。",
                        "narrative_intent": "让桌上的旧地图成为行动钩子。",
                    }
                ),
            ),
            patch(
                "src.dm.world_bridge.judge_trigger",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "src.dm.world_bridge.narrate_turn_final",
                new=AsyncMock(return_value="你可以先从桌上的旧地图和委托内容查起。"),
            ),
        ):
            payload = await SessionEngine().start_session(
                "guidance-test",
                scene_context,
                opening="我直接去拿还没发现的关键物品",
            )

        self.assertEqual(payload["status"], "awaiting_input")
        self.assertIn("旧地图", payload["say"])
        self.assertIsNone(payload["state"].get("world_writes"))


class LockedCombatIntentTests(unittest.IsolatedAsyncioTestCase):
    """确认 DM 一旦识别开战，修正载荷时不能偷偷降级成普通回复。"""

    async def test_invalid_combat_payload_cannot_downgrade_to_reply(self):
        scene = {
            "location": "村长家",
            "actors": [
                {
                    "actor_id": "elder_marlon",
                    "name": "村长马伦",
                    "disposition": "friendly",
                    "card": {"id": "elder_marlon"},
                }
            ],
        }
        decisions = AsyncMock(
            side_effect=[
                {
                    "intent": "start_combat",
                    "encounter": {"target_actor_ids": ["ghost_actor"]},
                },
                {"intent": "reply", "reply_brief": "请玩家再确认一次。"},
                {
                    "intent": "start_combat",
                    "encounter": {
                        "target_actor_ids": ["elder_marlon"],
                        "reason": "玩家主动攻击村长",
                    },
                },
            ]
        )

        with patch("src.dm.world_bridge._decide_llm", decisions):
            result = await world_bridge.decide_turn(
                "我拔剑攻击村长",
                scene,
                {},
            )

        self.assertEqual(result["intent"], "start_combat")
        self.assertEqual(
            result["encounter"]["target_actor_ids"],
            ["elder_marlon"],
        )
        self.assertEqual(decisions.await_count, 3)


class SpecialActionTests(unittest.TestCase):
    """验证线索只解锁优势，不成为进入战斗的空气墙。"""

    def setUp(self) -> None:
        get_registry().load_all()
        canon = get_registry().get("whispers_bell_tower")
        self.special_actions = list(
            canon.beat("bell_tower_summit").encounter.special_actions
        )
        self.player = PlayerCharacter.from_card(
            {
                "id": "pc_aldous",
                "name": "奥尔德斯",
                "strength": 16,
                "charisma": 14,
                "current_hp": 20,
                "max_hp": 20,
                "current_zone": "前排",
            }
        )
        self.player.controller = "user_aldous"
        self.player.inventory.append(
            InventoryItem(item_id="item_holy_water", quantity=1)
        )
        self.boss = Monster(
            id="bell_spirit",
            name="古钟之灵",
            current_hp=22,
            max_hp=22,
            ac=13,
            current_zone="前排",
            faction=Faction.ENEMY,
            attacks=[
                Attack(
                    name="回响重击",
                    attack_bonus=4,
                    damage_dice="1d8+2",
                    damage_type=DamageType.THUNDER,
                    attack_range=Range.MELEE,
                )
            ],
        )

    def _state(self, special_action_id: str) -> dict:
        return {
            "combatants": {
                self.player.id: self.player,
                self.boss.id: self.boss,
            },
            "initiative_order": [self.player.id, self.boss.id],
            "current_index": 0,
            "current_round": 1,
            "current_action": {
                "action_type": "special",
                "special_action_id": special_action_id,
                "target_id": self.boss.id,
            },
            "combat_log": [],
            "applied_special_actions": [],
            "scene_context": {
                "special_actions": self.special_actions,
                "story_flags": {
                    "clue_bell_crack": True,
                    "clue_spirit_name": True,
                    "clue_holy_water": True,
                },
            },
        }

    def test_no_clues_still_has_normal_actions_but_no_special_actions(self):
        self.player.inventory.clear()
        options = build_action_options(
            self.player,
            {self.player.id: self.player, self.boss.id: self.boss},
            special_actions=self.special_actions,
            story_flags=[],
        )

        self.assertTrue(options["natural_language"])
        self.assertTrue(options["pass"])
        self.assertEqual(options["special"], [])

    def test_all_three_preparations_unlock_their_fixed_actions(self):
        options = build_action_options(
            self.player,
            {self.player.id: self.player, self.boss.id: self.boss},
            special_actions=self.special_actions,
            story_flags=[
                "clue_bell_crack",
                "clue_spirit_name",
                "clue_holy_water",
            ],
        )

        self.assertEqual(
            {item["special_action_id"] for item in options["special"]},
            {"exploit_bell_crack", "invoke_true_name", "apply_holy_water"},
        )

    def test_crack_true_name_and_holy_water_apply_engine_effects(self):
        with patch("src.combat.nodes.interrupt", return_value={"d20": 20}):
            crack = resolve_action(self._state("exploit_bell_crack"))
        self.assertEqual(self.boss.ac, 11)
        self.assertIn("exploit_bell_crack", crack["applied_special_actions"])

        with patch("src.combat.nodes.interrupt", return_value={"d20": 20}):
            true_name = resolve_action(self._state("invoke_true_name"))
        self.assertTrue(self.boss.has_condition(ConditionType.STUNNED))
        self.assertIn("invoke_true_name", true_name["applied_special_actions"])

        holy_water = resolve_action(self._state("apply_holy_water"))
        self.assertEqual(self.boss.attacks[0].attack_bonus, 2)
        self.assertEqual(self.player.inventory[0].quantity, 0)
        self.assertIn("apply_holy_water", holy_water["applied_special_actions"])


class NaturalLanguageActionTests(unittest.TestCase):
    """验证自然语言只可落入引擎当前给出的行动集合。"""

    def test_llm_action_selection_is_revalidated_against_options(self):
        enemy = Monster(id="goblin", name="哥布林", current_hp=5, max_hp=5)
        options = {
            "attack": [
                {
                    "attack_name": "长剑",
                    "targets": [{"id": enemy.id, "name": enemy.name}],
                }
            ],
            "move": [{"target_zone": "后排"}],
            "special": [],
            "pass": True,
        }
        combatants = {enemy.id: enemy}

        accepted = validate_player_action(
            {
                "action_type": "attack",
                "attack_name": "长剑",
                "target_id": enemy.id,
            },
            options,
            combatants,
        )
        rejected = validate_player_action(
            {
                "action_type": "attack",
                "attack_name": "凭空出现的火球",
                "target_id": enemy.id,
            },
            options,
            combatants,
        )

        self.assertEqual(accepted["target_id"], enemy.id)
        self.assertIsNone(rejected)

    def test_api_accepts_natural_language_and_special_only_when_offered(self):
        natural = session_service.validate_action_resume(
            {"natural_language": True},
            {
                "action_type": "natural_language",
                "description": "我压低身形，用长剑横扫它的膝盖",
            },
        )
        special = session_service.validate_action_resume(
            {
                "special": [
                    {
                        "special_action_id": "invoke_true_name",
                        "target_id": "bell_spirit",
                    }
                ]
            },
            {
                "action_type": "special",
                "special_action_id": "invoke_true_name",
            },
        )

        self.assertEqual(natural["action_type"], "natural_language")
        self.assertEqual(special["target_id"], "bell_spirit")

    def test_combat_feed_contains_only_declarations_and_dm_narration(self):
        view = build_combat_view(
            {
                "combatants": {},
                "combat_log": [
                    {
                        "event": "combat_opening",
                        "text": "双方拔出武器，战斗一触即发。",
                    },
                    {
                        "event": "declaration",
                        "actor_id": "pc_aldous",
                        "text": "我用长剑逼退它。",
                    },
                    {"event": "attack", "actor": "pc_aldous", "hit": True},
                    {"event": "narration", "text": "剑锋擦过钟体。"},
                ],
            }
        )

        self.assertEqual(
            [item["role"] for item in view["feed"]], ["dm", "player", "dm"]
        )
        self.assertNotIn("hit", " ".join(item["content"] for item in view["feed"]))


class CombatTurnInputTests(unittest.IsolatedAsyncioTestCase):
    """验证战斗挂起期间不能绕过回合约束走普通消息接口。"""

    async def test_free_message_is_rejected_during_combat_interrupt(self):
        member = RoomMember(
            user_id="user_aldous",
            display_name="奥尔德斯玩家",
            character_id="pc_aldous",
            access_token="token",
            is_host=True,
        )
        room = GameRoom(
            room_code="COMBAT",
            campaign_id="whispers_bell_tower",
            status="playing",
            members={member.user_id: member},
        )

        class _InterruptedEngine:
            async def current_payload(self, _room_id):
                return {
                    "status": "interrupted",
                    "interrupt": {
                        "interrupt_type": "declare_action",
                        "directed_to": {"user_id": member.user_id},
                    },
                }

        previous_engine = session_service._engine
        previous_loaded = session_service._canon_loaded
        session_service._engine = _InterruptedEngine()
        session_service._canon_loaded = True
        try:
            with self.assertRaises(HTTPException) as raised:
                await session_service.message(room, member, "我在战斗外随便插一句话")
        finally:
            session_service._engine = previous_engine
            session_service._canon_loaded = previous_loaded
            session_service._room_locks.clear()

        self.assertEqual(raised.exception.status_code, 409)

    async def test_combat_opening_reaches_initiative_without_premature_hit(self):
        scene = {
            "random_seed": 7,
            "combatants": [
                {
                    "type": "player",
                    "controller": "user_aldous",
                    "card": {
                        "id": "pc_aldous",
                        "name": "奥尔德斯",
                        "current_hp": 12,
                        "max_hp": 12,
                        "attacks": [
                            {
                                "name": "长剑",
                                "attack_bonus": 5,
                                "damage_dice": "1d8+3",
                            }
                        ],
                    },
                },
                {
                    "type": "monster",
                    "card": {
                        "id": "bell_spirit",
                        "name": "古钟之灵",
                        "current_hp": 22,
                        "max_hp": 22,
                        "attacks": [
                            {
                                "name": "回响重击",
                                "attack_bonus": 4,
                                "damage_dice": "1d8+2",
                            }
                        ],
                    },
                },
            ],
        }
        with (
            patch(
                "src.combat.dm_bridge.judge_surprise_llm",
                AsyncMock(return_value=[]),
            ),
            patch(
                "src.combat.dm_bridge.narrate_combat_opening_llm",
                AsyncMock(return_value="双方在钟影下摆开架势，战斗正式开始。"),
            ),
        ):
            payload = await CombatEngine().start_combat("opening-test", scene)

        self.assertEqual(payload["status"], "interrupted")
        self.assertEqual(
            payload["interrupt"]["interrupt_type"],
            "roll_initiative",
        )
        feed = payload["interrupt"]["extra"]["combat"]["feed"]
        self.assertEqual(feed[0]["content"], "双方在钟影下摆开架势，战斗正式开始。")
        self.assertNotIn("命中", feed[0]["content"])


if __name__ == "__main__":
    unittest.main()
