"""Pydantic models for sessions and summaries."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RawSession(BaseModel):
    """Minimal representation of an opencode session row."""
    id: str
    project_id: str | None = None
    parent_id: str | None = None
    slug: str = ""
    title: str = ""
    agent: str | None = None
    model: str | None = None
    cost: float = 0.0
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_reasoning: int = 0
    tokens_cache_read: int = 0
    tokens_cache_write: int = 0
    metadata: str | None = None
    time_created: int = 0
    time_updated: int = 0
    workspace_id: str | None = None
    path: str | None = None


class RawMessage(BaseModel):
    """A single message from opencode.db."""
    id: str
    session_id: str
    time_created: int
    data: dict[str, Any]


class RawPart(BaseModel):
    """A part of a message (tool call, text, reasoning)."""
    id: str
    message_id: str
    session_id: str
    time_created: int
    data: dict[str, Any]


class RawProject(BaseModel):
    """Minimal project info."""
    id: str
    path: str | None = None
    directory: str | None = None


class SessionSummary(BaseModel):
    """Rich summary stored in our own DB."""
    session_id: str
    title: str = ""
    slug: str = ""
    agent: str | None = None
    model: str | None = None
    project_id: str | None = None
    project_path: str | None = None
    parent_id: str | None = None
    time_created: int = 0
    time_updated: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_total: int = 0
    cost: float = 0.0
    user_message_count: int = 0
    assistant_message_count: int = 0

    # Summarization
    goal: str = ""
    outcomes: str = ""
    decisions: list[str] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)
    tools_used: list[str] = Field(default_factory=list)
    summary_text: str = ""
    summary_version: int = 1

    # Timestamps in our DB
    created_at: int = 0
    updated_at: int = 0


class DailyDigest(BaseModel):
    """Per-day aggregation."""
    date: str
    session_count: int = 0
    total_tokens_input: int = 0
    total_tokens_output: int = 0
    total_cost: float = 0.0
    projects: list[str] = Field(default_factory=list)
    summary_text: str = ""


class ReflectionNote(BaseModel):
    """User note on a session."""
    id: int | None = None
    session_id: str
    note: str
    tags: list[str] = Field(default_factory=list)
    created_at: int = 0
