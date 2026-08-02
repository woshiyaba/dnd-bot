"""故事生成任务、中间产物、ID 预留与限时草稿的 SQLite 存储。"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

TASK_TTL = timedelta(hours=24)
DRAFT_TTL = timedelta(minutes=30)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    return json.loads(value)


class StoryGenerationStore:
    """使用短事务提供单进程 worker 所需的可恢复状态。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._closed = False
        self._initialize()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def __del__(self) -> None:
        """尽力关闭测试或嵌入式实例；生产实例仍由进程生命周期持有。"""
        try:
            self.close()
        except Exception:
            pass

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.executescript("""
                CREATE TABLE IF NOT EXISTS generation_tasks (
                    task_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    design_brief_json TEXT NOT NULL,
                    campaign_id TEXT,
                    draft_id TEXT,
                    error_public TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    repair_count INTEGER NOT NULL DEFAULT 0,
                    continuity_passed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS generation_artifacts (
                    task_id TEXT NOT NULL,
                    artifact_key TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    validated INTEGER NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, artifact_key),
                    FOREIGN KEY (task_id) REFERENCES generation_tasks(task_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS story_drafts (
                    draft_id TEXT PRIMARY KEY,
                    task_id TEXT,
                    campaign_id TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    quality_json TEXT,
                    expires_at TEXT NOT NULL,
                    published_at TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES generation_tasks(task_id) ON DELETE SET NULL
                );
                CREATE TABLE IF NOT EXISTS campaign_reservations (
                    campaign_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL UNIQUE,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES generation_tasks(task_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS generation_stage_attempts (
                    task_id TEXT NOT NULL,
                    stage_key TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, stage_key),
                    FOREIGN KEY (task_id) REFERENCES generation_tasks(task_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_generation_tasks_queue
                    ON generation_tasks(status, created_at);
                """)

    def recover_interrupted(self) -> int:
        """启动时把 running 恢复为 queued；validated artifacts 决定最近安全阶段。"""
        now = _iso(utc_now())
        with self._lock, self._connection:
            cancelled = self._connection.execute("""
                SELECT task_id FROM generation_tasks WHERE status='cancel_requested'
                """).fetchall()
            self._connection.execute(
                """
                UPDATE generation_tasks
                SET status='cancelled',stage='已取消',updated_at=?,completed_at=?
                WHERE status='cancel_requested'
                """,
                (now, now),
            )
            for row in cancelled:
                self._connection.execute(
                    "DELETE FROM campaign_reservations WHERE task_id=?",
                    (row["task_id"],),
                )
            cursor = self._connection.execute(
                """
                UPDATE generation_tasks
                SET status='queued', stage='恢复任务', updated_at=?
                WHERE status='running'
                """,
                (now,),
            )
            return cursor.rowcount

    def begin_stage_attempt(
        self, task_id: str, stage_key: str, *, max_attempts: int = 2
    ) -> int:
        """持久化阶段执行次数，防止同一未落库阶段在反复重启后无限重放。"""
        with self._lock, self._connection:
            row = self._connection.execute(
                """
                SELECT attempt_count FROM generation_stage_attempts
                WHERE task_id=? AND stage_key=?
                """,
                (task_id, stage_key),
            ).fetchone()
            current = int(row["attempt_count"]) if row is not None else 0
            if current >= max_attempts:
                raise RuntimeError(f"生成阶段 {stage_key} 已达到重启重试上限")
            next_attempt = current + 1
            self._connection.execute(
                """
                INSERT INTO generation_stage_attempts(task_id,stage_key,attempt_count,updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(task_id,stage_key) DO UPDATE SET
                    attempt_count=excluded.attempt_count,updated_at=excluded.updated_at
                """,
                (task_id, stage_key, next_attempt, _iso(utc_now())),
            )
            return next_attempt

    def create_task(self, task_id: str, design_brief: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO generation_tasks(
                    task_id,status,stage,progress,design_brief_json,created_at,updated_at
                ) VALUES(?, 'queued', '等待生成', 0, ?, ?, ?)
                """,
                (
                    task_id,
                    json.dumps(design_brief, ensure_ascii=False),
                    _iso(now),
                    _iso(now),
                ),
            )
        return self.get_task(task_id) or {}

    def next_queued_task(self) -> dict[str, Any] | None:
        with self._lock, self._connection:
            row = self._connection.execute("""
                SELECT * FROM generation_tasks
                WHERE status='queued' ORDER BY created_at LIMIT 1
                """).fetchone()
            if row is None:
                return None
            now = _iso(utc_now())
            changed = self._connection.execute(
                """
                UPDATE generation_tasks SET status='running', stage='恢复生成', updated_at=?
                WHERE task_id=? AND status='queued'
                """,
                (now, row["task_id"]),
            ).rowcount
            return self.get_task(row["task_id"]) if changed else None

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM generation_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return self._task_dict(row) if row is not None else None

    def update_task(
        self,
        task_id: str,
        *,
        stage: str,
        progress: int,
        repair_count: int | None = None,
    ) -> None:
        fields = ["stage=?", "progress=?", "updated_at=?"]
        values: list[Any] = [stage, max(0, min(100, progress)), _iso(utc_now())]
        if repair_count is not None:
            fields.append("repair_count=?")
            values.append(repair_count)
        values.append(task_id)
        with self._lock, self._connection:
            self._connection.execute(
                f"UPDATE generation_tasks SET {', '.join(fields)} WHERE task_id=?",
                values,
            )

    def save_artifact(
        self,
        task_id: str,
        *,
        stage: str,
        artifact_key: str,
        payload: dict[str, Any],
        attempt: int,
    ) -> None:
        """只保存已经通过本阶段校验的完整产物。"""
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO generation_artifacts(
                    task_id,artifact_key,stage,payload_json,validated,attempt,updated_at
                ) VALUES(?,?,?,?,1,?,?)
                ON CONFLICT(task_id, artifact_key) DO UPDATE SET
                    stage=excluded.stage,
                    payload_json=excluded.payload_json,
                    validated=1,
                    attempt=excluded.attempt,
                    updated_at=excluded.updated_at
                """,
                (
                    task_id,
                    artifact_key,
                    stage,
                    json.dumps(payload, ensure_ascii=False),
                    attempt,
                    _iso(utc_now()),
                ),
            )
            self._connection.execute(
                "DELETE FROM generation_stage_attempts WHERE task_id=? AND stage_key=?",
                (task_id, artifact_key),
            )

    def artifacts(self, task_id: str) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT artifact_key,payload_json FROM generation_artifacts
                WHERE task_id=? AND validated=1
                """,
                (task_id,),
            ).fetchall()
        return {row["artifact_key"]: json.loads(row["payload_json"]) for row in rows}

    def reserve_campaign_id(self, task_id: str, campaign_id: str) -> bool:
        """原子预留 ID；同一任务重复恢复视为成功。"""
        now = utc_now()
        expires_at = now + TASK_TTL
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT task_id FROM campaign_reservations WHERE campaign_id=?",
                (campaign_id,),
            ).fetchone()
            if existing is not None and existing["task_id"] != task_id:
                return False
            self._connection.execute(
                """
                INSERT INTO campaign_reservations(campaign_id,task_id,expires_at)
                VALUES(?,?,?)
                ON CONFLICT(campaign_id) DO UPDATE SET expires_at=excluded.expires_at
                """,
                (campaign_id, task_id, _iso(expires_at)),
            )
            self._connection.execute(
                "UPDATE generation_tasks SET campaign_id=?,updated_at=? WHERE task_id=?",
                (campaign_id, _iso(now), task_id),
            )
        return True

    def reserved_campaign_ids(self) -> list[str]:
        self.purge_expired()
        with self._lock:
            rows = self._connection.execute(
                "SELECT campaign_id FROM campaign_reservations ORDER BY campaign_id"
            ).fetchall()
        return [str(row["campaign_id"]) for row in rows]

    def request_cancel(self, task_id: str) -> dict[str, Any] | None:
        task = self.get_task(task_id)
        if task is None:
            return None
        if task["status"] in {"completed", "failed", "cancelled"}:
            return task
        if task["status"] == "queued":
            self.mark_cancelled(task_id)
            return self.get_task(task_id)
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE generation_tasks
                SET cancel_requested=1,status='cancel_requested',stage='正在取消',updated_at=?
                WHERE task_id=?
                """,
                (_iso(utc_now()), task_id),
            )
        return self.get_task(task_id)

    def is_cancel_requested(self, task_id: str) -> bool:
        task = self.get_task(task_id)
        return bool(task and task["cancel_requested"])

    def mark_cancelled(self, task_id: str) -> None:
        now = utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE generation_tasks
                SET status='cancelled',stage='已取消',updated_at=?,completed_at=?
                WHERE task_id=?
                """,
                (_iso(now), _iso(now), task_id),
            )
            self._connection.execute(
                "DELETE FROM campaign_reservations WHERE task_id=?", (task_id,)
            )

    def mark_failed(self, task_id: str, error_public: str) -> None:
        now = utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE generation_tasks SET
                    status='failed',stage='生成失败',error_public=?,updated_at=?,completed_at=?
                WHERE task_id=?
                """,
                (error_public[:1000], _iso(now), _iso(now), task_id),
            )
            self._connection.execute(
                "DELETE FROM campaign_reservations WHERE task_id=?", (task_id,)
            )

    def complete_task(
        self,
        task_id: str,
        *,
        draft_id: str,
        campaign_id: str,
        raw: dict[str, Any],
        quality: dict[str, Any],
    ) -> datetime:
        now = utc_now()
        expires_at = now + DRAFT_TTL
        with self._lock, self._connection:
            # 防止恢复过程重复创建或覆盖已经完成的草稿。
            existing = self._connection.execute(
                "SELECT draft_id FROM generation_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if existing is not None and existing["draft_id"]:
                row = self._connection.execute(
                    "SELECT expires_at FROM story_drafts WHERE draft_id=?",
                    (existing["draft_id"],),
                ).fetchone()
                if row is not None:
                    return datetime.fromisoformat(row["expires_at"])
            self._connection.execute(
                """
                INSERT INTO story_drafts(
                    draft_id,task_id,campaign_id,raw_json,quality_json,expires_at,created_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    draft_id,
                    task_id,
                    campaign_id,
                    json.dumps(raw, ensure_ascii=False),
                    json.dumps(quality, ensure_ascii=False),
                    _iso(expires_at),
                    _iso(now),
                ),
            )
            self._connection.execute(
                """
                UPDATE generation_tasks SET status='completed',stage='草稿已就绪',progress=100,
                    draft_id=?,continuity_passed=1,updated_at=?,completed_at=?
                WHERE task_id=?
                """,
                (draft_id, _iso(now), _iso(now), task_id),
            )
        return expires_at

    def create_compatibility_draft(
        self,
        *,
        draft_id: str,
        campaign_id: str,
        raw: dict[str, Any],
        quality: dict[str, Any] | None = None,
    ) -> datetime:
        now = utc_now()
        expires_at = now + DRAFT_TTL
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO story_drafts(
                    draft_id,task_id,campaign_id,raw_json,quality_json,expires_at,created_at
                ) VALUES(?,NULL,?,?,?,?,?)
                """,
                (
                    draft_id,
                    campaign_id,
                    json.dumps(raw, ensure_ascii=False),
                    json.dumps(quality, ensure_ascii=False) if quality else None,
                    _iso(expires_at),
                    _iso(now),
                ),
            )
        return expires_at

    def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        self.purge_expired()
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM story_drafts WHERE draft_id=? AND published_at IS NULL",
                (draft_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "draft_id": row["draft_id"],
            "task_id": row["task_id"],
            "campaign_id": row["campaign_id"],
            "raw": json.loads(row["raw_json"]),
            "quality": _loads(row["quality_json"]),
            "expires_at": datetime.fromisoformat(row["expires_at"]),
        }

    def mark_published(self, draft_id: str) -> None:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT task_id FROM story_drafts WHERE draft_id=?", (draft_id,)
            ).fetchone()
            self._connection.execute(
                "UPDATE story_drafts SET published_at=? WHERE draft_id=?",
                (_iso(utc_now()), draft_id),
            )
            if row is not None and row["task_id"]:
                self._connection.execute(
                    "DELETE FROM campaign_reservations WHERE task_id=?",
                    (row["task_id"],),
                )

    def purge_expired(self) -> None:
        now = utc_now()
        task_cutoff = now - TASK_TTL
        with self._lock, self._connection:
            expired_draft_tasks = self._connection.execute(
                """
                SELECT task_id FROM story_drafts
                WHERE published_at IS NULL AND expires_at<=? AND task_id IS NOT NULL
                """,
                (_iso(now),),
            ).fetchall()
            self._connection.execute(
                "DELETE FROM story_drafts WHERE published_at IS NULL AND expires_at<=?",
                (_iso(now),),
            )
            for row in expired_draft_tasks:
                self._connection.execute(
                    "DELETE FROM campaign_reservations WHERE task_id=?",
                    (row["task_id"],),
                )
            self._connection.execute(
                "DELETE FROM campaign_reservations WHERE expires_at<=?", (_iso(now),)
            )
            self._connection.execute(
                """
                DELETE FROM generation_tasks
                WHERE completed_at IS NOT NULL AND completed_at<=?
                """,
                (_iso(task_cutoff),),
            )

    @staticmethod
    def _task_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "task_id": row["task_id"],
            "status": row["status"],
            "stage": row["stage"],
            "progress": int(row["progress"]),
            "design_brief": json.loads(row["design_brief_json"]),
            "campaign_id": row["campaign_id"],
            "draft_id": row["draft_id"],
            "error": row["error_public"],
            "cancel_requested": bool(row["cancel_requested"]),
            "repair_count": int(row["repair_count"]),
            "continuity_passed": bool(row["continuity_passed"]),
            "created_at": datetime.fromisoformat(row["created_at"]),
            "updated_at": datetime.fromisoformat(row["updated_at"]),
            "completed_at": (
                datetime.fromisoformat(row["completed_at"])
                if row["completed_at"]
                else None
            ),
        }
