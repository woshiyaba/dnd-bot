"""DM 扩展包。

给战斗子图的 DM 节点提供三样东西，全部基于 LangChain 原生 API（不依赖 deepagents）：

- ``knowledge`` —— 本地 Markdown 知识库注册表（规则 / 怪物 / 技能），按需检索与读取；
- ``tools``     —— 暴露给 DM 的 LangChain 工具：骰子（d4–d20、roll_expr）+ 知识库查阅；
- ``prompt``    —— DM 人设与通用规则的常驻系统提示词；
- ``agent``     —— 用 ``langchain.agents.create_agent`` 装配并缓存的 DM 智能体，封装决策/叙述两类调用。

依赖方向：``src.combat`` 依赖本包，本包不反向依赖 ``src.combat``
（骰子工具通过 ``tools.set_dice_provider`` 由战斗层注入引擎骰子）。
"""
