"""会话层：把「中央 DM」与「战斗」编排成一整局可中断、可持久化的冒险。

依赖方向：``session`` 可同时依赖 ``combat``（规则/中断/战斗子图）与 ``dm``（DM 智能体/世界桥接），
``combat`` 与 ``dm`` 互不依赖、都只依赖 ``model``。会话层是唯一允许「同时拉两边」的拼装层。

对外入口：:class:`src.session.engine.SessionEngine`。
"""
