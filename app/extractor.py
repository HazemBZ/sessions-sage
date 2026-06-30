"""Read-only access to opencode.db."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class OpenCodeExtractor:
    """Read-only extractor of sessions from OpenCode's SQLite database."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            if not self.db_path.exists():
                raise FileNotFoundError(f"opencode.db not found: {self.db_path}")
            self._conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ---- Session queries ----

    def get_updated_sessions(self, since_epoch_ms: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        """Return sessions updated after `since_epoch_ms`.

        Returns newest-first, capped at `limit`. Use `since_epoch_ms=0` for all.
        """
        rows = self.conn.execute(
            """SELECT * FROM session
               WHERE time_updated > ?
               ORDER BY time_updated ASC
               LIMIT ?""",
            (since_epoch_ms, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM session WHERE id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_recent_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM session ORDER BY time_created DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_session_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) as cnt FROM session").fetchone()
        return row["cnt"] if row else 0

    def get_sessions_for_project(self, project_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM session WHERE project_id = ? ORDER BY time_created DESC LIMIT ?",
            (project_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- Message queries ----

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Return all messages for a session, ordered by time_created."""
        rows = self.conn.execute(
            "SELECT * FROM message WHERE session_id = ? ORDER BY time_created ASC",
            (session_id,),
        ).fetchall()

        result = []
        for r in rows:
            d = dict(r)
            try:
                d["data"] = json.loads(d.get("data") or "{}")
            except (json.JSONDecodeError, TypeError):
                d["data"] = {}
            result.append(d)
        return result

    def get_messages_count(self, session_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM message WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row["cnt"] if row else 0

    # ---- Part queries ----

    def get_parts(self, session_id: str) -> list[dict[str, Any]]:
        """Return all parts for a session's messages."""
        rows = self.conn.execute(
            "SELECT * FROM part WHERE session_id = ? ORDER BY time_created ASC",
            (session_id,),
        ).fetchall()

        result = []
        for r in rows:
            d = dict(r)
            try:
                d["data"] = json.loads(d.get("data") or "{}")
            except (json.JSONDecodeError, TypeError):
                d["data"] = {}
            result.append(d)
        return result

    # ---- Project queries ----

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM project WHERE id = ?", (project_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_all_projects(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM project").fetchall()
        return [dict(r) for r in rows]

    # ---- Aggregation queries ----

    def get_session_stats(self) -> dict[str, Any]:
        """Aggregate stats across all sessions."""
        row = self.conn.execute("""
            SELECT
                COUNT(*) as total_sessions,
                COALESCE(SUM(tokens_input), 0) as total_tokens_input,
                COALESCE(SUM(tokens_output), 0) as total_tokens_output,
                COALESCE(SUM(tokens_reasoning), 0) as total_tokens_reasoning,
                COALESCE(SUM(cost), 0) as total_cost,
                COALESCE(SUM(tokens_cache_read), 0) as total_cache_read,
                COALESCE(SUM(tokens_cache_write), 0) as total_cache_write
            FROM session
        """).fetchone()
        return dict(row) if row else {}

    def get_daily_session_counts(self, days: int = 30) -> list[dict[str, Any]]:
        cutoff = int(time.time() * 1000) - days * 86400 * 1000
        rows = self.conn.execute("""
            SELECT
                date(time_created / 1000, 'unixepoch') as day,
                COUNT(*) as session_count
            FROM session
            WHERE time_created > ?
            GROUP BY day
            ORDER BY day DESC
        """, (cutoff,)).fetchall()
        return [dict(r) for r in rows]

    def get_child_sessions(self, parent_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM session WHERE parent_id = ? ORDER BY time_created ASC",
            (parent_id,),
        ).fetchall()
        return [dict(r) for r in rows]
