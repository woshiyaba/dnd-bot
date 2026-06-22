from __future__ import annotations

import os
from typing import Any

import aiomysql
from dotenv import load_dotenv

load_dotenv()

_pool: aiomysql.Pool | None = None


def _get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"缺少 MySQL 环境变量：{name}")
    return value


def _get_mysql_port() -> int:
    raw_port = os.getenv("MYSQL_PORT", "3306")
    try:
        return int(raw_port)
    except ValueError as exc:
        raise RuntimeError(f"MYSQL_PORT 必须是整数，当前值：{raw_port}") from exc


async def get_mysql_pool() -> aiomysql.Pool:
    """获取全局 MySQL 连接池，首次调用时懒加载创建。"""
    global _pool
    if _pool is None:
        _pool = await aiomysql.create_pool(
            host=_get_required_env("MYSQL_HOST"),
            port=_get_mysql_port(),
            db=_get_required_env("MYSQL_DATABASE"),
            user=_get_required_env("MYSQL_USER"),
            password=_get_required_env("MYSQL_PASSWORD"),
            charset="utf8mb4",
            autocommit=True,
            cursorclass=aiomysql.DictCursor,
        )
    return _pool


async def fetch_one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    """执行查询并返回第一行结果。"""
    pool = await get_mysql_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(sql, params)
            return await cursor.fetchone()


async def close_mysql_pool() -> None:
    """关闭连接池，主要用于测试或应用退出时清理资源。"""
    global _pool
    if _pool is None:
        return
    _pool.close()
    await _pool.wait_closed()
    _pool = None
