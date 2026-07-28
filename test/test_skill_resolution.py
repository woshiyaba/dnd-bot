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
    """常见伤害、治疗与主动特性必须生成结构化模板。"""

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


if __name__ == "__main__":
    unittest.main()
