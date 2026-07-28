"""结构化技能计划、职业特性与引擎结算测试。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.character.creation import build_character_card
from src.character.progression import grant_experience
from src.combat import skill_resolver as skill_resolver_module
from src.combat.interrupts import build_action_options
from src.combat.nodes import (
    _clear_concentration_effects,
    _resolve_skill,
    _turn_action_budget,
    resolve_action,
    route_after_check,
)
from src.combat.skill_resolver import prepare_skill_plan, validate_skill_plan
from src.model.combatant import Monster, PlayerCharacter
from src.model.effects import Condition
from src.model.enums import CombatOutcome, ConditionType

BASE = {
    "strength": 15,
    "dexterity": 14,
    "constitution": 15,
    "intelligence": 8,
    "wisdom": 10,
    "charisma": 8,
}


def _paladin() -> PlayerCharacter:
    return PlayerCharacter.from_card(
        build_character_card(
            character_id="pc_paladin",
            name="圣武士",
            race_id="human",
            class_id="paladin",
            base_abilities=BASE,
        )
    )


class SkillPlanTests(unittest.IsolatedAsyncioTestCase):
    async def test_skill_agent_uses_embedded_system_prompt(self):
        previous_agent = skill_resolver_module._cached_agent
        fake_model = object()
        fake_agent = object()
        skill_resolver_module._cached_agent = None
        try:
            with (
                patch.object(
                    skill_resolver_module,
                    "create_chat_model",
                    return_value=fake_model,
                ),
                patch.object(
                    skill_resolver_module,
                    "create_agent",
                    return_value=fake_agent,
                ) as create_agent,
                patch.object(
                    skill_resolver_module,
                    "register_system_prompt",
                ) as register_prompt,
            ):
                agent = await skill_resolver_module._get_agent()

            self.assertIs(agent, fake_agent)
            create_agent.assert_called_once_with(
                fake_model,
                tools=[],
                system_prompt=skill_resolver_module._SYSTEM_PROMPT,
            )
            register_prompt.assert_called_once_with(
                "combat_skill_resolver",
                skill_resolver_module._SYSTEM_PROMPT,
            )
        finally:
            skill_resolver_module._cached_agent = previous_agent

    async def test_divine_smite_plan_is_deterministic_and_structured(self):
        actor = _paladin()
        target = Monster.from_card(
            {"id": "goblin", "name": "哥布林", "current_hp": 20, "max_hp": 20}
        )
        plan = await prepare_skill_plan(
            actor=actor,
            skill_id="feature_divine_smite",
            combatants={actor.id: actor, target.id: target},
            selected_target_ids=[target.id],
            current_round=1,
        )
        self.assertEqual(plan["schema_version"], 1)
        self.assertEqual(plan["roll"]["kind"], "attack_roll")
        self.assertEqual(plan["effects"][0]["dice"], "2d8")

    async def test_unknown_effect_is_rejected(self):
        actor = _paladin()
        target = Monster.from_card(
            {"id": "goblin", "name": "哥布林", "current_hp": 20, "max_hp": 20}
        )
        with self.assertRaises(ValueError):
            validate_skill_plan(
                {
                    "schema_version": 1,
                    "skill_id": "feature_divine_smite",
                    "roll": {"kind": "none"},
                    "effects": [{"kind": "set_final_hp", "target_id": target.id}],
                },
                actor=actor,
                combatants={actor.id: actor, target.id: target},
                selected_target_ids=[target.id],
            )

    async def test_catalog_spell_uses_llm_json_protocol(self):
        actor = PlayerCharacter.from_card(
            build_character_card(
                character_id="pc_cleric",
                name="牧师",
                race_id="human",
                class_id="cleric",
                base_abilities=BASE,
            )
        )
        target = Monster.from_card(
            {"id": "goblin", "name": "哥布林", "current_hp": 20, "max_hp": 20}
        )
        fake_agent = SimpleNamespace(
            ainvoke=AsyncMock(
                return_value={
                    "messages": [
                        SimpleNamespace(
                            content=(
                                '{"schema_version":1,"skill_id":"sacred_flame",'
                                '"summary":"敏捷豁免，失败受到光耀伤害",'
                                '"roll":{"kind":"saving_throw","ability":"dexterity"},'
                                '"effects":[{"kind":"damage","target_id":"goblin",'
                                '"dice":"1d8","damage_type":"radiant",'
                                '"on_save":"none"}]}'
                            )
                        )
                    ]
                }
            )
        )
        with patch(
            "src.combat.skill_resolver._get_agent",
            new=AsyncMock(return_value=fake_agent),
        ):
            plan = await prepare_skill_plan(
                actor=actor,
                skill_id="sacred_flame",
                combatants={actor.id: actor, target.id: target},
                selected_target_ids=[target.id],
                current_round=1,
            )
        self.assertEqual(plan["roll"]["kind"], "saving_throw")
        self.assertEqual(plan["effects"][0]["damage_type"], "radiant")


class SkillExecutionTests(unittest.TestCase):
    def test_engine_rolls_damage_and_starts_cooldown(self):
        actor = _paladin()
        actor.is_player_controlled = False
        target = Monster.from_card(
            {"id": "goblin", "name": "哥布林", "current_hp": 40, "max_hp": 40}
        )
        state = {
            "combatants": {actor.id: actor, target.id: target},
            "initiative_order": [actor.id, target.id],
            "current_index": 0,
            "current_round": 1,
            "pending_skill_plan": {
                "schema_version": 1,
                "skill_id": "feature_divine_smite",
                "summary": "测试伤害",
                "roll": {"kind": "none"},
                "effects": [
                    {
                        "kind": "damage",
                        "target_id": target.id,
                        "dice": "2d8",
                        "damage_type": "radiant",
                        "on_save": "full",
                    }
                ],
            },
        }
        events = _resolve_skill(
            state,
            actor,
            {
                "action_type": "skill",
                "skill_id": "feature_divine_smite",
                "target_id": target.id,
            },
            state["combatants"],
        )
        self.assertLess(target.current_hp, 40)
        self.assertTrue(any(event["event"] == "skill_damage" for event in events))
        smite = next(
            skill for skill in actor.skills if skill.skill_id == "feature_divine_smite"
        )
        self.assertEqual(smite.cooldown_left, 2)

    def test_action_surge_and_extra_attack_stack(self):
        fighter = PlayerCharacter.from_card(
            build_character_card(
                character_id="pc_fighter",
                name="战士",
                race_id="human",
                class_id="fighter",
                base_abilities=BASE,
            )
        )
        self.assertEqual(_turn_action_budget(fighter), 2)
        fighter.level = 5
        self.assertEqual(_turn_action_budget(fighter), 4)
        fighter.level = 11
        self.assertEqual(_turn_action_budget(fighter), 5)

    def test_extra_attack_is_not_a_second_general_action(self):
        cleric = PlayerCharacter.from_card(
            build_character_card(
                character_id="pc_cleric",
                name="牧师",
                race_id="human",
                class_id="cleric",
                base_abilities=BASE,
            )
        )
        grant_experience(cleric, 6500)
        cleric.is_player_controlled = False
        enemy = Monster.from_card(
            {"id": "goblin", "name": "哥布林", "current_hp": 30, "max_hp": 30}
        )
        base_state = {
            "combatants": {cleric.id: cleric, enemy.id: enemy},
            "initiative_order": [cleric.id, enemy.id],
            "current_index": 0,
            "current_round": 1,
            "actions_remaining": 1,
            "extra_attacks_remaining": 1,
            "attack_action_started": False,
            "outcome": CombatOutcome.ONGOING,
        }

        moved = resolve_action(
            {
                **base_state,
                "current_action": {"action_type": "move", "target_zone": "后排"},
            }
        )
        self.assertEqual(
            route_after_check(
                {**base_state, **moved, "outcome": CombatOutcome.ONGOING}
            ),
            "next_turn",
        )

        cleric.current_zone = enemy.current_zone
        attacked = resolve_action(
            {
                **base_state,
                "current_action": {
                    "action_type": "attack",
                    "attack_name": cleric.attacks[0].name,
                    "target_id": enemy.id,
                },
            }
        )
        after_attack = {
            **base_state,
            **attacked,
            "outcome": CombatOutcome.ONGOING,
        }
        self.assertEqual(route_after_check(after_attack), "same_turn")
        self.assertEqual(after_attack["actions_remaining"], 0)
        self.assertTrue(after_attack["attack_action_started"])
        options = build_action_options(
            cleric,
            after_attack["combatants"],
            actions_remaining=0,
            extra_attacks_remaining=1,
            attack_action_started=True,
        )
        self.assertTrue(options["attack_only"])
        self.assertNotIn("skill", options)
        self.assertEqual(options["move"], [])

    def test_revival_effect_accepts_and_restores_a_down_ally(self):
        cleric = PlayerCharacter.from_card(
            build_character_card(
                character_id="pc_cleric",
                name="牧师",
                race_id="human",
                class_id="cleric",
                base_abilities=BASE,
            )
        )
        grant_experience(cleric, 6500)
        cleric.is_player_controlled = False
        ally = _paladin()
        ally.take_damage(ally.max_hp)
        state = {
            "combatants": {cleric.id: cleric, ally.id: ally},
            "initiative_order": [cleric.id, ally.id],
            "current_index": 0,
            "current_round": 1,
            "pending_skill_plan": {
                "schema_version": 1,
                "skill_id": "revivify",
                "summary": "让倒地同伴恢复 1 点生命",
                "roll": {"kind": "none"},
                "effects": [
                    {
                        "kind": "revive",
                        "target_id": ally.id,
                        "amount": 1,
                        "on_save": "full",
                    }
                ],
            },
        }
        events = _resolve_skill(
            state,
            cleric,
            {
                "action_type": "skill",
                "skill_id": "revivify",
                "target_ids": [ally.id],
            },
            state["combatants"],
        )
        self.assertTrue(ally.is_alive)
        self.assertEqual(ally.current_hp, 1)
        self.assertTrue(any(event["event"] == "revive" for event in events))

    def test_same_spell_from_two_casters_has_isolated_concentration(self):
        first = _paladin()
        second = _paladin()
        second.id = "pc_paladin_2"
        target = Monster.from_card(
            {"id": "goblin", "name": "哥布林", "current_hp": 20, "max_hp": 20}
        )
        for caster in (first, second):
            target.add_condition(
                Condition(
                    kind=ConditionType.BUFF,
                    rounds_left=3,
                    source_skill_id="bless",
                    source_actor_id=caster.id,
                )
            )
        combatants = {first.id: first, second.id: second, target.id: target}
        _clear_concentration_effects(combatants, "bless", first.id)
        self.assertEqual(len(target.conditions), 1)
        self.assertEqual(target.conditions[0].source_actor_id, second.id)


if __name__ == "__main__":
    unittest.main()
