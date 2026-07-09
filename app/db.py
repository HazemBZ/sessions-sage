"""Local SQLite database for storing summaries and user reflections."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS session_summary (
    session_id        TEXT PRIMARY KEY,
    title             TEXT DEFAULT '',
    slug              TEXT DEFAULT '',
    agent             TEXT,
    model             TEXT,
    project_id        TEXT,
    project_path      TEXT,
    parent_id         TEXT,
    time_created      INTEGER DEFAULT 0,
    time_updated      INTEGER DEFAULT 0,
    tokens_input      INTEGER DEFAULT 0,
    tokens_output     INTEGER DEFAULT 0,
    tokens_total      INTEGER DEFAULT 0,
    cost              REAL DEFAULT 0,
    user_message_count  INTEGER DEFAULT 0,
    assistant_message_count INTEGER DEFAULT 0,
    goal              TEXT DEFAULT '',
    outcomes          TEXT DEFAULT '',
    decisions         TEXT DEFAULT '[]',
    files_changed     TEXT DEFAULT '[]',
    tools_used        TEXT DEFAULT '[]',
    summary_text      TEXT DEFAULT '',
    summary_version   INTEGER DEFAULT 1,
    created_at        INTEGER DEFAULT 0,
    updated_at        INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS daily_digest (
    date              TEXT PRIMARY KEY,
    session_count     INTEGER DEFAULT 0,
    total_tokens_input   INTEGER DEFAULT 0,
    total_tokens_output  INTEGER DEFAULT 0,
    total_cost        REAL DEFAULT 0,
    projects          TEXT DEFAULT '[]',
    summary_text      TEXT DEFAULT '',
    created_at        INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS reflection_note (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        TEXT NOT NULL,
    note              TEXT NOT NULL,
    tags              TEXT DEFAULT '[]',
    created_at        INTEGER DEFAULT 0,
    FOREIGN KEY (session_id) REFERENCES session_summary(session_id)
);

CREATE TABLE IF NOT EXISTS extractor_cursor (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    last_checked      INTEGER DEFAULT 0,
    last_session_id   TEXT DEFAULT ''
);

-- indexes for common queries
CREATE INDEX IF NOT EXISTS idx_summary_time_created ON session_summary(time_created DESC);
CREATE INDEX IF NOT EXISTS idx_summary_project ON session_summary(project_id);
CREATE INDEX IF NOT EXISTS idx_summary_agent ON session_summary(agent);
CREATE INDEX IF NOT EXISTS idx_note_session ON reflection_note(session_id);
"""


class SummaryDB:
    """Local database for summaries and notes."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn

    def initialize(self) -> None:
        """Create tables if they don't exist + run migrations."""
        self.conn.executescript(SCHEMA_SQL)
        self._run_migrations()
        self.conn.commit()

    def _run_migrations(self) -> None:
        """Add columns that may not exist in older schemas."""
        migrations = [
            "ALTER TABLE session_summary ADD COLUMN discussion_summary TEXT DEFAULT ''",
            "ALTER TABLE session_summary ADD COLUMN discussion_summary_version INTEGER DEFAULT 0",
        ]
        for sql in migrations:
            try:
                self.conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # column already exists

    # ---- Cursor ----

    def get_cursor(self) -> tuple[int, str]:
        """Return (last_checked_epoch_ms, last_session_id)."""
        row = self.conn.execute(
            "SELECT last_checked, last_session_id FROM extractor_cursor WHERE id = 1"
        ).fetchone()
        if row is None:
            return (0, "")
        return (row["last_checked"], row["last_session_id"] or "")

    def update_cursor(self, checked_at: int, last_id: str) -> None:
        self.conn.execute(
            """INSERT INTO extractor_cursor (id, last_checked, last_session_id)
               VALUES (1, ?, ?)
               ON CONFLICT(id) DO UPDATE SET last_checked=excluded.last_checked,
                                              last_session_id=excluded.last_session_id""",
            (checked_at, last_id),
        )
        self.conn.commit()

    # ---- Session Summary ----

    def upsert_summary(self, row: dict[str, Any]) -> None:
        now = int(time.time() * 1000)
        row.setdefault("created_at", now)
        row.setdefault("updated_at", now)
        row.setdefault("summary_version", 1)

        cols = ", ".join(row.keys())
        placeholders = ", ".join("?" for _ in row)
        update_cols = ", ".join(f"{k}=excluded.{k}" for k in row if k != "session_id")

        self.conn.execute(
            f"""INSERT INTO session_summary ({cols})
                VALUES ({placeholders})
                ON CONFLICT(session_id) DO UPDATE SET {update_cols}""",
            list(row.values()),
        )
        self.conn.commit()

    def get_summary(self, session_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM session_summary WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def get_summaries(
        self,
        limit: int = 50,
        offset: int = 0,
        project_id: str | None = None,
        agent: str | None = None,
        model: str | None = None,
        days: int | None = None,
        search: str | None = None,
        parent_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []

        if project_id:
            clauses.append("(project_id = ? OR (project_id = 'global' AND project_path = ?))")
            params.extend([project_id, project_id])
        if agent:
            clauses.append("agent = ?")
            params.append(agent)
        if model:
            clauses.append("model LIKE ?")
            params.append(f"%\"id\":\"{model}\"%")
        if days:
            cutoff = int(time.time() * 1000) - days * 86400 * 1000
            clauses.append("time_created >= ?")
            params.append(cutoff)
        if search:
            clauses.append("(title LIKE ? OR goal LIKE ? OR summary_text LIKE ? OR outcomes LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like, like])
        if parent_id is not None:
            clauses.append("parent_id IS ?" if parent_id == "" else "parent_id = ?")
            params.append(parent_id if parent_id != "" else None)

        where = " AND ".join(clauses) if clauses else "1"

        rows = self.conn.execute(
            f"SELECT * FROM session_summary WHERE {where} ORDER BY time_created DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [dict(r) for r in rows]

    def count_summaries(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) as cnt FROM session_summary").fetchone()
        return row["cnt"] if row else 0

    # ---- Daily digest ----

    def upsert_daily_digest(self, row: dict[str, Any]) -> None:
        now = int(time.time() * 1000)
        row.setdefault("created_at", now)
        cols = ", ".join(row.keys())
        placeholders = ", ".join("?" for _ in row)
        update = ", ".join(f"{k}=excluded.{k}" for k in row if k != "date")
        self.conn.execute(
            f"INSERT INTO daily_digest ({cols}) VALUES ({placeholders}) ON CONFLICT(date) DO UPDATE SET {update}",
            list(row.values()),
        )
        self.conn.commit()

    def get_daily_digests(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM daily_digest ORDER BY date DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_daily_digest(self, date: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM daily_digest WHERE date = ?", (date,)
        ).fetchone()
        return dict(row) if row else None

    # ---- Notes ----

    def add_note(self, session_id: str, note: str, tags: list[str] | None = None) -> int:
        now = int(time.time() * 1000)
        import json
        cur = self.conn.execute(
            "INSERT INTO reflection_note (session_id, note, tags, created_at) VALUES (?, ?, ?, ?)",
            (session_id, note, json.dumps(tags or []), now),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_notes(self, session_id: str) -> list[dict[str, Any]]:
        import json
        rows = self.conn.execute(
            "SELECT * FROM reflection_note WHERE session_id = ? ORDER BY created_at DESC",
            (session_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["tags"] = json.loads(d.get("tags", "[]"))
            except (json.JSONDecodeError, TypeError):
                d["tags"] = []
            result.append(d)
        return result

    def get_summaries_without_discussion_summary(
        self, limit: int = 10, target_version: int = 1
    ) -> list[dict[str, Any]]:
        """Return sessions that need LLM discussion summary (missing or stale)."""
        rows = self.conn.execute(
            """SELECT session_id, title, goal, user_message_count, assistant_message_count
               FROM session_summary
               WHERE discussion_summary_version IS NULL
                  OR discussion_summary_version < ?
               ORDER BY time_created DESC
               LIMIT ?""",
            (target_version, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_discussion_summary(
        self, session_id: str, summary_text: str, version: int = 1
    ) -> None:
        now = int(time.time() * 1000)
        self.conn.execute(
            """UPDATE session_summary
               SET discussion_summary = ?, discussion_summary_version = ?, updated_at = ?
               WHERE session_id = ?""",
            (summary_text, version, now, session_id),
        )
        self.conn.commit()

    def update_session_title(self, session_id: str, title: str) -> None:
        now = int(time.time() * 1000)
        self.conn.execute(
            "UPDATE session_summary SET title = ?, updated_at = ? WHERE session_id = ?",
            (title, now, session_id),
        )
        self.conn.commit()

    # ---- Aggregation helpers ----

    def rebuild_daily_digests(self) -> None:
        """Aggregate session_summary into daily_digest for the last 90 days."""
        import json

        rows = self.conn.execute("""
            SELECT
                date(time_created / 1000, 'unixepoch') as day,
                COUNT(*) as session_count,
                SUM(tokens_input) as total_input,
                SUM(tokens_output) as total_output,
                SUM(cost) as total_cost
            FROM session_summary
            WHERE time_created > ?
            GROUP BY day
            ORDER BY day DESC
        """, (int(time.time() * 1000) - 90 * 86400 * 1000,)).fetchall()

        for r in rows:
            # collect distinct projects for the day
            project_rows = self.conn.execute(
                "SELECT DISTINCT project_path FROM session_summary WHERE date(time_created / 1000, 'unixepoch') = ? AND project_path IS NOT NULL",
                (r["day"],),
            ).fetchall()
            projects = list({p["project_path"] for p in project_rows if p["project_path"]})

            self.upsert_daily_digest({
                "date": r["day"],
                "session_count": r["session_count"],
                "total_tokens_input": r["total_input"] or 0,
                "total_tokens_output": r["total_output"] or 0,
                "total_cost": r["total_cost"] or 0.0,
                "projects": json.dumps(projects),
            })

    # ---- Stats ----

    def get_stats(self) -> dict[str, Any]:
        row = self.conn.execute("""
            SELECT
                COUNT(*) as total_sessions,
                COALESCE(SUM(tokens_input), 0) as total_tokens_input,
                COALESCE(SUM(tokens_output), 0) as total_tokens_output,
                COALESCE(SUM(cost), 0) as total_cost,
                COALESCE(SUM(user_message_count), 0) as total_user_msgs,
                COALESCE(SUM(assistant_message_count), 0) as total_assistant_msgs
            FROM session_summary
        """).fetchone()
        stats = dict(row) if row else {}

        disc_row = self.conn.execute("""
            SELECT
                COUNT(*) as total
            FROM session_summary
        """).fetchone()
        disc_done = self.conn.execute("""
            SELECT COUNT(*) as cnt FROM session_summary
            WHERE discussion_summary IS NOT NULL AND discussion_summary != ''
        """).fetchone()
        stats["discussion_done"] = disc_done["cnt"] if disc_done else 0
        stats["discussion_pending"] = (disc_row["total"] if disc_row else 0) - stats["discussion_done"]

        title_m = self.conn.execute("""
            SELECT COUNT(*) as cnt FROM session_summary
            WHERE title IS NULL OR title = '' OR title = 'Untitled' OR title = 'Session'
               OR title LIKE 'New session%'
               OR title LIKE '____-__-__T%'
               OR title LIKE '____-__-__'
        """).fetchone()
        stats["title_backfill_pending"] = title_m["cnt"] if title_m else 0
        return stats

    def get_agents(self) -> list[tuple[str, int]]:
        rows = self.conn.execute(
            "SELECT agent, COUNT(*) as cnt FROM session_summary WHERE agent IS NOT NULL GROUP BY agent ORDER BY cnt DESC"
        ).fetchall()
        return [(r["agent"], r["cnt"]) for r in rows]

    def get_models(self) -> list[tuple[str, int]]:
        """Return (model_id, count) tuples, parsed from JSON model field."""
        rows = self.conn.execute(
            "SELECT model, COUNT(*) as cnt FROM session_summary WHERE model IS NOT NULL AND model != '' GROUP BY model ORDER BY cnt DESC"
        ).fetchall()
        import json
        aggregated: dict[str, int] = {}
        for r in rows:
            try:
                parsed = json.loads(r["model"])
                mid = parsed.get("id", r["model"])
                aggregated[mid] = aggregated.get(mid, 0) + r["cnt"]
            except (json.JSONDecodeError, TypeError):
                mid = r["model"]
                aggregated[mid] = aggregated.get(mid, 0) + r["cnt"]
        return sorted(aggregated.items(), key=lambda x: -x[1])

    def get_projects(self) -> list[tuple[str, str, int]]:
        """Return (project_id, project_path, session_count) sorted.

        Groups global sessions by actual directory path so they appear as
        separate project entries instead of one giant "global" bucket.
        """
        rows = self.conn.execute("""
            SELECT project_id, project_path, COUNT(*) as cnt
            FROM session_summary
            WHERE project_id IS NOT NULL AND project_id != 'global'
            GROUP BY project_id
            ORDER BY cnt DESC
        """).fetchall()
        result = [(r["project_id"], r["project_path"] or "", r["cnt"]) for r in rows]

        # Break global sessions into per-directory groups
        global_rows = self.conn.execute("""
            SELECT project_path, COUNT(*) as cnt
            FROM session_summary
            WHERE project_id = 'global' AND project_path NOT NULL AND project_path != ''
            GROUP BY project_path
            ORDER BY cnt DESC
        """).fetchall()
        for r in global_rows:
            result.append((r["project_path"], r["project_path"], r["cnt"]))

        return result

    def get_project_sessions(
        self, project_id: str, limit: int = 200, offset: int = 0, sort: str = "recent"
    ) -> list[dict[str, Any]]:
        if sort == "cost":
            order = "ORDER BY cost DESC"
        elif sort == "oldest":
            order = "ORDER BY time_created ASC"
        else:  # recent (default)
            order = "ORDER BY time_created DESC"

        rows = self.conn.execute(
            f"""SELECT * FROM session_summary
               WHERE project_id = ?
                  OR (project_id = 'global' AND project_path = ?)
               {order}
               LIMIT ? OFFSET ?""",
            (project_id, project_id, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_project_stats(self, project_id: str) -> dict[str, Any]:
        row = self.conn.execute("""
            SELECT
                COUNT(*) as total_sessions,
                MIN(time_created) as first_session,
                MAX(time_created) as last_session,
                MAX(project_path) as project_path,
                COALESCE(SUM(tokens_input), 0) as total_tokens_input,
                COALESCE(SUM(tokens_output), 0) as total_tokens_output,
                COALESCE(SUM(cost), 0) as total_cost,
                SUM(CASE WHEN discussion_summary IS NOT NULL AND discussion_summary != '' THEN 1 ELSE 0 END) as discussion_done
            FROM session_summary WHERE project_id = ?
               OR (project_id = 'global' AND project_path = ?)
        """, (project_id, project_id)).fetchone()
        result = dict(row) if row else {}
        agents = self.conn.execute(
            "SELECT agent, COUNT(*) as cnt FROM session_summary WHERE (project_id = ? OR (project_id = 'global' AND project_path = ?)) AND agent IS NOT NULL GROUP BY agent ORDER BY cnt DESC",
            (project_id, project_id),
        ).fetchall()
        result["agents"] = [dict(a) for a in agents]
        models = self.conn.execute(
            "SELECT model, COUNT(*) as cnt FROM session_summary WHERE (project_id = ? OR (project_id = 'global' AND project_path = ?)) AND model IS NOT NULL AND model != '' GROUP BY model ORDER BY cnt DESC",
            (project_id, project_id),
        ).fetchall()
        import json
        model_list: list[dict[str, Any]] = []
        seen: dict[str, int] = {}
        for m in models:
            try:
                parsed = json.loads(m["model"])
                mid = parsed.get("id", m["model"])
                seen[mid] = seen.get(mid, 0) + m["cnt"]
            except (json.JSONDecodeError, TypeError):
                seen[m["model"]] = seen.get(m["model"], 0) + m["cnt"]
        for mid, cnt in sorted(seen.items(), key=lambda x: -x[1]):
            model_list.append({"model": mid, "cnt": cnt})
        result["models"] = model_list
        return result

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
