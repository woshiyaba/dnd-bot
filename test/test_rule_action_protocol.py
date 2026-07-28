"""统一规则行动协议的纯规则与安全边界测试。"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from src.combat.action_compiler import prepare_action_plan, validate_action_plan
from src.combat.action_executor import (
    commit_action_cost,
    execute_combat_plan,
    execute_world_plan,
    preflight_world_effects,
)
from src.combat.nodes import enter_combat
from src.model.combatant import Monster, PlayerCharacter
from src.model.effects import InventoryItem
from src.model.rule_action import ActionDefinition
from src.model.dm_state import build_beat_scene, init_story
from src.session import action_nodes, story_nodes
from src.story.loader import get_registry


def _hero() -> PlayerCharacter:
    hero = PlayerCharacter.from_card(
        {
            "id": "pc_hero",
            "name": "测试圣武士",
            "class_id": "paladin",
            "char_class": "圣武士",
            "level": 2,
            "current_hp": 20,
            "max_hp": 20,
            "ac": 16,
            "strength": 16,
            "dexterity": 10,
            "constitution": 14,
            "intelligence": 10,
            "wisdom": 12,
            "charisma": 16,
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
    hero.controller = "user_test"
    return hero


def _enemy() -> Monster:
    return Monster.from_card(
        {
            "id": "serpent",
            "name": "火蟒",
            "current_hp": 30,
            "max_hp": 30,
            "ac": 14,
            "attacks": [
                {
                    "name": "撕咬",
                    "attack_bonus": 4,
                    "damage_dice": "1d6+2",
                    "damage_type": "piercing",
                    "range": "melee",
                }
            ],
        }
    )


def _frost_definition() -> ActionDefinition:
    return ActionDefinition.from_dict(
        {
            "id": "apply_frost",
            "name": "寒冰符箓",
            "source_kind": "item",
            "source_ref": "item_frost",
            "scopes": ["combat"],
            "targeting": {"min_targets": 1, "max_targets": 1},
            "usage": {"kind": "consume_item", "item_id": "item_frost"},
            "contract": {
                "check_templates": [],
                "effect_templates": [
                    {
                        "id": "lower_ac",
                        "kind": "modify_ac",
                        "target_mode": "selected_one",
                        "amount": -3,
                        "rounds": 3,
                        "when": {"outcomes": ["always"]},
                    }
                ],
            },
        }
    )


class RuleActionProtocolTests(unittest.IsolatedAsyncioTestCase):
    """验证计划白名单、提交幂等性与战斗/世界执行。"""

    def test_template_parameters_override_llm_invention(self):
        hero, enemy = _hero(), _enemy()
        definition = _frost_definition()
        plan = validate_action_plan(
            {
                "schema_version": 2,
                "definition_id": definition.id,
                "actor_id": hero.id,
                "selected_target_ids": [enemy.id],
                "summary": "冻结鳞片",
                "checks": [],
                "effects": [
                    {
                        "id": "invented",
                        "template_id": "lower_ac",
                        "target_id": enemy.id,
                        "amount": -10,
                    }
                ],
            },
            definition=definition,
            actor=hero,
            targets={hero.id: hero, enemy.id: enemy},
            selected_target_ids=[enemy.id],
            scope="combat",
        )
        self.assertEqual(plan["effects"][0]["amount"], -3)

    def test_unknown_effect_is_rejected_before_cost(self):
        with self.assertRaisesRegex(ValueError, "不支持 scope combat"):
            ActionDefinition.from_dict(
                {
                    "id": "unsafe",
                    "name": "非法效果",
                    "source_kind": "quest_feature",
                    "source_ref": "unsafe",
                    "scopes": ["combat"],
                    "targeting": {"min_targets": 1, "max_targets": 1},
                    "contract": {
                        "check_templates": [],
                        "effect_templates": [
                            {
                                "id": "hp",
                                "kind": "set_final_hp",
                                "target_mode": "selected_one",
                            }
                        ],
                    },
                }
            )

    def test_once_per_combat_cannot_leak_into_world_scope(self):
        with self.assertRaisesRegex(ValueError, "只能用于 combat"):
            ActionDefinition.from_dict(
                {
                    "id": "bad_world_usage",
                    "name": "错误次数域",
                    "source_kind": "quest_feature",
                    "source_ref": "bad_world_usage",
                    "scopes": ["world"],
                    "usage": {"kind": "once_per_combat"},
                    "contract": {
                        "check_templates": [],
                        "effect_templates": [
                            {
                                "id": "flag",
                                "kind": "set_flag",
                                "target_mode": "none",
                                "flag": "done",
                            }
                        ],
                    },
                }
            )

    def test_combat_carries_session_usage_into_local_registry(self):
        hero, enemy = _hero(), _enemy()
        initialized = enter_combat(
            {
                "combatants": {hero.id: hero, enemy.id: enemy},
                "scene_context": {"used_session_rule_actions": ["quest.once_only"]},
            }
        )
        self.assertEqual(initialized["used_rule_actions"], ["quest.once_only"])

    def test_commit_is_idempotent_and_combat_effect_is_real(self):
        hero, enemy = _hero(), _enemy()
        hero.inventory.append(InventoryItem("item_frost", 1))
        definition = _frost_definition()
        plan = validate_action_plan(
            {
                "schema_version": 2,
                "definition_id": definition.id,
                "actor_id": hero.id,
                "selected_target_ids": [enemy.id],
                "checks": [],
                "effects": [
                    {"id": "lower", "template_id": "lower_ac", "target_id": enemy.id}
                ],
            },
            definition=definition,
            actor=hero,
            targets={hero.id: hero, enemy.id: enemy},
            selected_target_ids=[enemy.id],
            scope="combat",
        )
        used, committed, _ = commit_action_cost(hero, plan, [], [])
        used, committed, replay = commit_action_cost(hero, plan, used, committed)
        self.assertEqual(hero.inventory[0].quantity, 0)
        self.assertEqual(replay["event"], "action_commit_replayed")

        events = execute_combat_plan(
            {"combatants": {hero.id: hero, enemy.id: enemy}, "combat_log": []},
            hero,
            plan,
        )
        self.assertEqual(enemy.ac, 11)
        self.assertTrue(any(event["event"] == "modify_ac" for event in events))

    def test_insufficient_item_cost_does_not_mutate_inventory(self):
        hero = _hero()
        hero.inventory.append(InventoryItem("item_frost", 1))
        plan = {
            "definition_id": "apply_frost",
            "plan_id": "plan_insufficient",
            "source_kind": "item",
            "source_ref": "item_frost",
            "usage": {
                "kind": "consume_item",
                "item_id": "item_frost",
                "quantity": 2,
            },
        }
        with self.assertRaisesRegex(ValueError, "数量不足"):
            commit_action_cost(hero, plan, [], [])
        self.assertEqual(hero.inventory[0].quantity, 1)

    def test_world_preflight_aggregates_removals_before_mutation(self):
        hero = _hero()
        hero.inventory.append(InventoryItem("item_key", 1))
        effects = [
            {
                "id": "remove_one",
                "kind": "remove_item",
                "item_id": "item_key",
                "quantity": 1,
                "target_id": hero.id,
            },
            {
                "id": "remove_two",
                "kind": "remove_item",
                "item_id": "item_key",
                "quantity": 1,
                "target_id": hero.id,
            },
        ]
        with self.assertRaisesRegex(ValueError, "数量不足"):
            preflight_world_effects(effects, hero, {hero.id: hero})
        self.assertEqual(hero.inventory[0].quantity, 1)

    def test_world_action_emits_engine_write(self):
        hero = _hero()
        definition = ActionDefinition.from_dict(
            {
                "id": "use_key",
                "name": "使用铜钥",
                "source_kind": "item",
                "source_ref": "item_key",
                "scopes": ["world"],
                "targeting": {"min_targets": 0, "max_targets": 0},
                "usage": {"kind": "unlimited"},
                "contract": {
                    "check_templates": [],
                    "effect_templates": [
                        {
                            "id": "open",
                            "kind": "transition_beat",
                            "target_mode": "none",
                            "beat_id": "vault",
                        }
                    ],
                },
            }
        )
        plan = validate_action_plan(
            {
                "schema_version": 2,
                "definition_id": definition.id,
                "actor_id": hero.id,
                "selected_target_ids": [],
                "checks": [],
                "effects": [{"id": "open", "template_id": "open"}],
            },
            definition=definition,
            actor=hero,
            targets={hero.id: hero},
            selected_target_ids=[],
            scope="world",
        )
        _, writes = execute_world_plan({"party": {hero.id: hero}}, hero, plan)
        self.assertEqual(writes, {"transition_to_beat_id": "vault"})

    async def test_llm_failure_never_consumes_resource(self):
        hero, enemy = _hero(), _enemy()
        hero.inventory.append(InventoryItem("item_frost", 1))
        fake_agent = AsyncMock()
        fake_agent.ainvoke.return_value = {"messages": []}
        with patch(
            "src.combat.action_compiler._get_agent",
            new=AsyncMock(return_value=fake_agent),
        ):
            with self.assertRaisesRegex(ValueError, "编译失败"):
                await prepare_action_plan(
                    definition=_frost_definition(),
                    actor=hero,
                    targets={hero.id: hero, enemy.id: enemy},
                    selected_target_ids=[enemy.id],
                    scope="combat",
                )
        self.assertEqual(fake_agent.ainvoke.await_count, 3)
        self.assertEqual(hero.inventory[0].quantity, 1)

    async def test_copper_key_world_chain_reaches_legal_exit(self):
        canon = get_registry().load_all()["prodigal_return_quest"]
        hero = _hero()
        hero.inventory.append(InventoryItem("item_copper_key", 1))
        story, _ = init_story(canon)
        story.update(
            {
                "current_beat_id": "gate_exploration",
                "current_location_id": "inscription_hall",
                "visited_beats": ["trail_opening", "gate_exploration"],
                "visited_locations": [
                    "mountain_trail",
                    "ruined_gate",
                    "inscription_hall",
                ],
            }
        )
        state = {
            "campaign_id": canon.campaign_id,
            "story": story,
            "scene": build_beat_scene(
                canon,
                canon.beat("gate_exploration"),
                location_id="inscription_hall",
            ),
            "party": {hero.id: hero},
            "active_actor_id": hero.id,
            "active_user_id": "user_test",
            "active_display_name": "测试玩家",
            "structured_action": {
                "action_id": "use_copper_key",
                "target_ids": [],
            },
            "used_rule_actions": [],
            "committed_action_plans": [],
            "campaign_log": [],
            "messages": [],
        }
        definition = canon.action_definition("use_copper_key")
        plan = validate_action_plan(
            {
                "schema_version": 2,
                "definition_id": definition.id,
                "actor_id": hero.id,
                "selected_target_ids": [],
                "checks": [],
                "effects": [{"id": "open", "template_id": "open_chamber"}],
            },
            definition=definition,
            actor=hero,
            targets={hero.id: hero},
            selected_target_ids=[],
            scope="world",
        )
        state["pending_action_plan"] = plan
        state.update(action_nodes.commit_world_action(state))
        state.update(action_nodes.execute_world_action(state))
        advancement = await story_nodes.evaluate_advancement(state)
        self.assertEqual(advancement["next_story"], "advance")
        self.assertEqual(advancement["story"]["pending_next_beat_id"], "chamber_climax")


if __name__ == "__main__":
    unittest.main()
