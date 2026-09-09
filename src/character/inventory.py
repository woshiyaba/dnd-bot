"""背包通用物品、规则行动和队友间转交。"""

from __future__ import annotations

from src.model.combatant import Character
from src.model.effects import InventoryItem
from src.model.rule_action import ActionDefinition

ITEM_NAMES = {
    "item_healing_potion": "治疗药水",
    "item_gold": "金币",
    "item_holy_water": "圣水",
}

HEALING_POTION = ActionDefinition.from_dict(
    {
        "id": "item.healing_potion",
        "name": "使用治疗药水",
        "source_kind": "item",
        "source_ref": "item_healing_potion",
        "scopes": ["world", "combat"],
        "description": "饮用或喂给同一区域的队友，恢复 2d4+2 点生命；消耗一瓶，战斗中占用一个行动。",
        "targeting": {
            "faction": "ally",
            "life_state": "any",
            "range": "melee",
            "min_targets": 1,
            "max_targets": 1,
        },
        "usage": {"kind": "consume_item", "quantity": 1},
        "contract": {
            "effect_templates": [
                {
                    "id": "healing",
                    "kind": "healing",
                    "target_mode": "selected_one",
                    "dice": "2d4+2",
                }
            ]
        },
    }
)


def transfer_item(
    sender: Character, recipient: Character, item_id: str, quantity: int
) -> None:
    """全部验证通过后转交现有物品，不凭客户端声明创建物品。"""
    if sender.id == recipient.id:
        raise ValueError("不能把物品转交给自己")
    if not sender.is_alive or not recipient.is_alive:
        raise ValueError("只有能行动的角色可以交接物品")
    if type(quantity) is not int or quantity < 1:
        raise ValueError("转交数量必须是正整数")
    owned = next((item for item in sender.inventory if item.item_id == item_id), None)
    if owned is None or owned.quantity < quantity:
        raise ValueError("背包物品数量不足")
    received = next(
        (item for item in recipient.inventory if item.item_id == item_id), None
    )
    if received is None:
        recipient.inventory.append(InventoryItem(item_id=item_id, quantity=quantity))
    else:
        received.quantity += quantity
    owned.quantity -= quantity
