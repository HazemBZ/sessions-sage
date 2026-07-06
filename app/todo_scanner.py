"""Scan, parse, and cache TODO artifact files (.omo/todos/).

Each TODO artifact is a Markdown file with YAML frontmatter.
Format (see TODO_SCHEMA.md):
  .omo/todos/<YYYY-MM-DD>_<kebab-label>.md

  ---
  title: str       (required)
  date: str        (required, YYYY-MM-DD)
  status: str      (required, pending|in_progress|completed|cancelled)
  tags: [str]      (optional)
  depends_on: [str] (optional)
  session_id: str  (optional)
  items:
    - id: str          (required)
      description: str (required)
      status: str      (required)
      priority: str    (optional, high|medium|low)
      files: [str]     (optional)
      notes: str       (optional)
  ---
  ## Body (free-form markdown)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ── Models ──────────────────────────────────────────────────────────────────


@dataclass
class TodoItem:
    """A single item inside a TODO artifact."""

    id: str
    description: str
    status: str  # pending | in_progress | completed | cancelled
    priority: str | None = None
    files: list[str] = field(default_factory=list)
    notes: str | None = None


@dataclass
class TodoArtifact:
    """A parsed TODO artifact file."""

    # File metadata
    file_path: str
    project_path: str
    file_name: str
    last_modified: float

    # Frontmatter (required)
    title: str
    date: str  # YYYY-MM-DD
    status: str  # pending | in_progress | completed | cancelled

    # Frontmatter (optional)
    tags: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    session_id: str | None = None
    items: list[TodoItem] = field(default_factory=list)

    # Body
    body_markdown: str = ""

    # Derived
    @property
    def project_short(self) -> str:
        """Last component of project path for display."""
        return Path(self.project_path).name if self.project_path else self.project_path

    @property
    def item_counts(self) -> dict[str, int]:
        """Count items by status."""
        counts: dict[str, int] = {"pending": 0, "in_progress": 0, "completed": 0, "cancelled": 0}
        for item in self.items:
            s = item.status if item.status in counts else "pending"
            counts[s] += 1
        return counts

    @property
    def total_items(self) -> int:
        return len(self.items)

    @property
    def completed_items(self) -> int:
        return sum(1 for i in self.items if i.status == "completed")

    @property
    def progress_pct(self) -> float:
        if not self.items:
            return 0.0
        return (self.completed_items / self.total_items) * 100.0

    @property
    def is_stale(self) -> bool:
        """True if status=completed but not all items completed, or vice versa."""
        if self.status == "completed" and self.completed_items < self.total_items:
            return True
        if self.status in ("pending", "in_progress") and self.completed_items == self.total_items and self.total_items > 0:
            return True
        return False


# ── Parser ──────────────────────────────────────────────────────────────────


def parse_todo_file(file_path: str, project_path: str) -> TodoArtifact | None:
    """Parse a single TODO artifact .md file into a TodoArtifact.

    Returns None if the file is malformed or missing required fields.
    """
    path = Path(file_path)
    if not path.exists():
        logger.warning("TODO file not found: %s", file_path)
        return None

    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to read TODO file %s: %s", file_path, exc)
        return None

    # Split frontmatter from body
    # Frontmatter is between first pair of --- lines
    if not raw.startswith("---"):
        logger.warning("TODO file %s missing YAML frontmatter (---)", file_path)
        return None

    # Find closing ---
    second_dash = raw.find("---", 3)
    if second_dash == -1:
        logger.warning("TODO file %s missing closing ---", file_path)
        return None

    frontmatter_text = raw[3:second_dash].strip()
    body_text = raw[second_dash + 3:].strip()

    # Parse YAML
    try:
        data: dict[str, Any] = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as exc:
        logger.warning("YAML parse error in %s: %s", file_path, exc)
        return None

    # Validate required fields
    missing = [f for f in ("title", "date", "status") if f not in data]
    if missing:
        logger.warning("TODO file %s missing required fields: %s", file_path, ", ".join(missing))
        return None

    # Parse items
    raw_items = data.get("items", []) or []
    items: list[TodoItem] = []
    for i, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, dict):
            continue
        if "id" not in raw_item or "description" not in raw_item or "status" not in raw_item:
            logger.warning("TODO file %s item %d missing required fields (id, description, status)", file_path, i)
            continue
        items.append(TodoItem(
            id=str(raw_item["id"]),
            description=str(raw_item["description"]),
            status=str(raw_item["status"]),
            priority=str(raw_item["priority"]) if raw_item.get("priority") else None,
            files=raw_item.get("files") or [],
            notes=str(raw_item["notes"]) if raw_item.get("notes") else None,
        ))

    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0

    return TodoArtifact(
        file_path=file_path,
        project_path=project_path,
        file_name=path.name,
        last_modified=mtime,
        title=str(data["title"]),
        date=str(data["date"]),
        status=str(data["status"]),
        tags=data.get("tags") or [],
        depends_on=data.get("depends_on") or [],
        session_id=str(data["session_id"]) if data.get("session_id") else None,
        items=items,
        body_markdown=body_text,
    )


# ── Scanner ─────────────────────────────────────────────────────────────────


@dataclass
class ScanResult:
    """Result of a scan across multiple projects."""

    artifacts: list[TodoArtifact]
    errors: list[str]
    scanned_at: float
    project_count: int


def scan_projects(project_paths: list[str]) -> ScanResult:
    """Scan a list of project directories for .omo/todos/*.md files.

    Each path is a project root directory. The scanner looks for
    .omo/todos/*.md relative to each project root.
    """
    artifacts: list[TodoArtifact] = []
    errors: list[str] = []
    scanned_paths: set[str] = set()

    for project_path in project_paths:
        if not project_path or project_path in scanned_paths:
            continue
        scanned_paths.add(project_path)

        todos_dir = Path(project_path) / ".omo" / "todos"
        if not todos_dir.is_dir():
            continue

        try:
            for md_file in sorted(todos_dir.glob("*.md")):
                if md_file.name == "TODO_SCHEMA.md":
                    continue  # skip schema definition
                artifact = parse_todo_file(str(md_file), project_path)
                if artifact is not None:
                    artifacts.append(artifact)
                else:
                    errors.append(f"Failed to parse: {md_file}")
        except Exception as exc:
            errors.append(f"Error scanning {todos_dir}: {exc}")

    # Sort: newest date first, then by filename
    artifacts.sort(key=lambda a: (a.date, a.file_name), reverse=True)

    return ScanResult(
        artifacts=artifacts,
        errors=errors,
        scanned_at=time.time(),
        project_count=len(scanned_paths),
    )


# ── Cache ───────────────────────────────────────────────────────────────────


class TodoCache:
    """Simple time-based cache for scan results.

    Thread-safe for GIL-protected operations (single-threaded access
    is the norm with FastAPI + APScheduler).
    """

    def __init__(self, ttl_seconds: float = 60.0):
        self._ttl = ttl_seconds
        self._result: ScanResult | None = None
        self._cached_at: float = 0.0

    @property
    def is_expired(self) -> bool:
        return self._result is None or (time.time() - self._cached_at) > self._ttl

    def get(self) -> ScanResult | None:
        if self.is_expired:
            return None
        return self._result

    def set(self, result: ScanResult) -> None:
        self._result = result
        self._cached_at = time.time()

    def invalidate(self) -> None:
        self._result = None
        self._cached_at = 0.0

    def get_or_scan(self, project_paths: list[str]) -> ScanResult:
        """Return cached result if fresh, otherwise scan."""
        cached = self.get()
        if cached is not None:
            return cached
        result = scan_projects(project_paths)
        self.set(result)
        return result


# Global cache instance (used by routes and scheduler)
CACHE = TodoCache(ttl_seconds=60.0)
