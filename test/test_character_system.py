"""角色创建、技能解锁与经验成长的纯规则测试。"""

from __future__ import annotations

import unittest

from src.character.creation import build_character_card, point_buy_cost
from src.character.progression import (
    apply_ability_increases,
    grant_experience,
    level_for_experience,
)
from src.character.skills import spell_unlock_character_level, unlocked_spell_ids
from src.combat.nodes import settle
from src.model.combatant import PlayerCharacter
from src.model.enums import CombatOutcome

BASE = {
    "strength": 15,
    "dexterity": 14,
    "constitution": 15,
    "intelligence": 8,
    "wisdom": 10,
    "charisma": 8,
}


class CharacterCreationTests(unittest.TestCase):
    def test_standard_point_buy_and_human_bonus(self):
        self.assertEqual(point_buy_cost(BASE), 27)
        card = build_character_card(
            character_id="pc_test",
            name="测试者",
            race_id="human",
            class_id="fighter",
            base_abilities=BASE,
        )
        self.assertEqual(card["strength"], 16)
        self.assertEqual(card["constitution"], 16)
        self.assertEqual(card["max_hp"], 13)
        self.assertEqual(card["ac"], 18)

    def test_half_elf_requires_two_distinct_non_charisma_choices(self):
        with self.assertRaises(ValueError):
            build_character_card(
                character_id="pc_test",
                name="测试者",
                race_id="half_elf",
                class_id="bard",
                base_abilities=BASE,
                racial_bonus_choices=["strength"],
            )
        card = build_character_card(
            character_id="pc_test",
            name="测试者",
            race_id="half_elf",
            class_id="bard",
            base_abilities=BASE,
            racial_bonus_choices=["dexterity", "constitution"],
        )
        self.assertEqual(card["dexterity"], 15)
        self.assertEqual(card["constitution"], 16)
        self.assertEqual(card["charisma"], 10)

    def test_bard_feature_overrides_all_initial_skill_cooldowns(self):
        card = build_character_card(
            character_id="pc_bard",
            name="诗人",
            race_id="human",
            class_id="bard",
            base_abilities=BASE,
        )
        self.assertIn("feature_font_of_inspiration", card["features"])
        self.assertTrue(card["skills"])
        self.assertTrue(all(skill["cooldown_rounds"] == 0 for skill in card["skills"]))


class ProgressionTests(unittest.TestCase):
    def test_standard_threshold_and_automatic_unlock(self):
        card = build_character_card(
            character_id="pc_paladin",
            name="圣武士",
            race_id="human",
            class_id="paladin",
            base_abilities=BASE,
        )
        character = PlayerCharacter.from_card(card)
        self.assertEqual(level_for_experience(299), 1)
        summary = grant_experience(character, 300)
        self.assertEqual(character.level, 2)
        self.assertGreater(character.max_hp, card["max_hp"])
        self.assertEqual(summary["new_level"], 2)
        learned = {skill.skill_id for skill in character.skills}
        self.assertIn("feature_divine_smite", learned)
        self.assertTrue(set(unlocked_spell_ids("paladin", 2)).issubset(learned))
        first_level_spell = next(
            skill for skill in character.skills if skill.source_type == "spell"
        )
        self.assertEqual(first_level_spell.unlock_level, 2)
        self.assertEqual(spell_unlock_character_level("cleric", 2), 3)

    def test_ability_increase_shape_and_cap(self):
        character = PlayerCharacter.from_card(
            build_character_card(
                character_id="pc_test",
                name="测试者",
                race_id="human",
                class_id="fighter",
                base_abilities=BASE,
            )
        )
        character.pending_ability_points = 2
        before = character.strength
        apply_ability_increases(character, {"strength": 2})
        self.assertEqual(character.strength, before + 2)
        self.assertEqual(character.pending_ability_points, 0)

    def test_legacy_character_without_experience_never_downgrades(self):
        character = PlayerCharacter.from_card(
            build_character_card(
                character_id="pc_legacy",
                name="旧角色",
                race_id="human",
                class_id="fighter",
                base_abilities=BASE,
            )
        )
        character.level = 3
        summary = grant_experience(character, 0)
        self.assertEqual(character.level, 3)
        self.assertEqual(summary["new_level"], 3)

    def test_level_up_does_not_revive_a_down_character(self):
        character = PlayerCharacter.from_card(
            build_character_card(
                character_id="pc_down",
                name="倒地角色",
                race_id="human",
                class_id="fighter",
                base_abilities=BASE,
            )
        )
        character.take_damage(character.max_hp)
        grant_experience(character, 300)
        self.assertEqual(character.current_hp, 0)
        self.assertFalse(character.is_alive)

    def test_combat_settlement_awards_configured_experience_once_per_character(self):
        character = PlayerCharacter.from_card(
            build_character_card(
                character_id="pc_reward",
                name="获奖角色",
                race_id="human",
                class_id="paladin",
                base_abilities=BASE,
            )
        )
        result = settle(
            {
                "combatants": {character.id: character},
                "outcome": CombatOutcome.PLAYERS_WIN,
                "scene_context": {"xp_reward": 300, "loot_table": []},
                "combat_log": [],
            }
        )
        self.assertEqual(character.experience, 300)
        self.assertEqual(character.level, 2)
        growth = result["scene_context"]["growth"][character.id]
        self.assertEqual(growth["experience_gained"], 300)
        self.assertEqual(result["scene_context"]["writeback"][character.id]["level"], 2)


if __name__ == "__main__":
    unittest.main()
