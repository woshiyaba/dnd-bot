"""技能适配到统一规则行动协议的回归测试。"""

from __future__ import annotations

import unittest

from src.character.skills import learned_skills_for_class
from src.combat.action_registry import skill_action_definition
from src.model.combatant import PlayerCharacter


def _character(class_id: str, level: int = 3) -> PlayerCharacter:
    actor = PlayerCharacter.from_card(
        {
            "id": f"pc_{class_id}",
            "name": class_id,
            "class_id": class_id,
            "level": level,
            "current_hp": 20,
            "max_hp": 20,
            "skills": [
                skill.to_dict() for skill in learned_skills_for_class(class_id, level)
            ],
        }
    )
    return actor


class SkillAdapterTests(unittest.TestCase):
    """只有完整建模的技能可以生成结构化模板。"""

    def test_sacred_flame_uses_save_and_damage_templates(self):
        actor = _character("cleric")
        definition, reason = skill_action_definition(actor, "sacred_flame")
        self.assertIsNone(reason)
        self.assertIsNotNone(definition)
        self.assertEqual(
            definition.contract["check_templates"][0]["kind"], "saving_throw"
        )
        self.assertEqual(definition.contract["effect_templates"][0]["kind"], "damage")

    def test_healing_spell_uses_healing_template(self):
        actor = _character("cleric")
        definition, reason = skill_action_definition(actor, "cure_wounds")
        self.assertIsNone(reason)
        self.assertEqual(definition.contract["effect_templates"][0]["kind"], "healing")

    def test_divine_smite_is_a_rule_action(self):
        actor = _character("paladin")
        definition, reason = skill_action_definition(actor, "feature_divine_smite")
        self.assertIsNone(reason)
        self.assertEqual(definition.source_kind, "skill")
        self.assertEqual(definition.id, "skill.feature_divine_smite")
        self.assertEqual(
            definition.contract["check_templates"][0]["bonus_source"],
            "weapon_attack",
        )

    def test_only_explicit_skill_whitelist_is_enabled(self):
        expected = {
            "feature_divine_smite",
            "sacred_flame",
            "cure_wounds",
            "revivify",
            "healing_word",
            "inflict_wounds",
            "mass_cure_wounds",
            "mass_healing_word",
            "harm",
            "heal",
        }
        actors = [
            _character("bard", 20),
            _character("cleric", 20),
            _character("paladin", 20),
        ]
        enabled: set[str] = set()
        for actor in actors:
            for skill in actor.skills:
                definition, _ = skill_action_definition(actor, skill.skill_id)
                if definition is not None:
                    enabled.add(skill.skill_id)

        self.assertEqual(enabled, expected)

    def test_complex_spells_stay_disabled(self):
        actors = {
            "vicious_mockery": _character("bard", 20),
            "thunderwave": _character("bard", 20),
            "spirit_guardians": _character("cleric", 20),
        }
        for skill_id, actor in actors.items():
            with self.subTest(skill_id=skill_id):
                definition, reason = skill_action_definition(actor, skill_id)
                self.assertIsNone(definition)
                self.assertEqual(reason, "该技能尚未完成规则建模")

    def test_sacred_flame_scales_by_character_level(self):
        for level, expected in ((1, "1d8"), (5, "2d8"), (11, "3d8"), (17, "4d8")):
            with self.subTest(level=level):
                definition, _ = skill_action_definition(
                    _character("cleric", level), "sacred_flame"
                )
                self.assertEqual(
                    definition.contract["effect_templates"][0]["dice"], expected
                )

    def test_explicit_healing_and_damage_contracts(self):
        actor = _character("cleric", 20)
        cases = {
            "healing_word": ("healing", "1d4", 1),
            "mass_cure_wounds": ("healing", "3d8", 6),
            "mass_healing_word": ("healing", "1d4", 6),
            "inflict_wounds": ("damage", "3d10", 1),
            "harm": ("damage", "14d6", 1),
        }
        for skill_id, (kind, dice, maximum) in cases.items():
            with self.subTest(skill_id=skill_id):
                definition, reason = skill_action_definition(actor, skill_id)
                self.assertIsNone(reason)
                self.assertEqual(definition.targeting["max_targets"], maximum)
                self.assertEqual(
                    definition.contract["effect_templates"][0]["kind"], kind
                )
                self.assertEqual(
                    definition.contract["effect_templates"][0]["dice"], dice
                )
                self.assertEqual(definition.usage["kind"], "skill_resource")

        heal, _ = skill_action_definition(actor, "heal")
        self.assertEqual(heal.contract["effect_templates"][0]["amount"], 70)
        revivify, _ = skill_action_definition(actor, "revivify")
        self.assertEqual(revivify.targeting["life_state"], "down")
        self.assertEqual(revivify.contract["effect_templates"][0]["kind"], "revive")


if __name__ == "__main__":
    unittest.main()
