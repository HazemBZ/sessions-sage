"""Rule-based session summarizer + LLM discussion summary.

Extracts goals, decisions, outcomes, files changed, and tools used
from raw session data without requiring an LLM.
The `summarize_discussion_llm()` function uses Ollama for narrative summaries.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_MEANINGLESS_TITLE_PATTERNS: list[re.Pattern] = [
    re.compile(r'^New session\s*[-–—]\s*\d{4}', re.IGNORECASE),
    re.compile(r'^New session$', re.IGNORECASE),
    re.compile(r'^Untitled$', re.IGNORECASE),
    re.compile(r'^Session$', re.IGNORECASE),
    re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}', re.IGNORECASE),
    re.compile(r'^\d{4}-\d{2}-\d{2}$', re.IGNORECASE),
]


def is_meaningless_title(title: str) -> bool:
    if not title or not title.strip():
        return True
    t = title.strip()
    if any(p.search(t) for p in _MEANINGLESS_TITLE_PATTERNS):
        return True
    return False


# Tools that indicate file modification
WRITE_TOOLS = {"write", "edit", "rewrite", "ast_grep_replace", "create"}
READ_TOOLS = {"read", "grep", "glob", "ast_grep_search", "search"}
GIT_TOOLS = {"bash"}  # checked for git commands
TOOL_ACTION_KEYWORDS = [
    "mkdir", "touch", "mv ", "cp ", "rm ", "chmod",
]


def _parse_role(msg: dict[str, Any]) -> str:
    """Extract role from message data JSON."""
    d = msg.get("data", {})
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except (json.JSONDecodeError, TypeError):
            return "unknown"
    return d.get("role", "unknown") if isinstance(d, dict) else "unknown"


def _get_text_content(msg: dict[str, Any]) -> str:
    """Extract text content from a message's data."""
    d = msg.get("data", {})
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except (json.JSONDecodeError, TypeError):
            return d
    if isinstance(d, dict):
        return d.get("text", d.get("content", "")) or ""
    return str(d)


def _get_first_user_message(messages: list[dict[str, Any]]) -> str:
    """Find the first meaningful user message to extract the goal."""
    for msg in messages:
        if _parse_role(msg) == "user":
            text = _get_text_content(msg)
            text = _strip_tool_mentions(text)
            if len(text) > 20:
                return text[:2000]
    return ""


def _get_last_assistant_message(messages: list[dict[str, Any]]) -> str:
    """Find the last meaningful assistant message for outcomes."""
    for msg in reversed(messages):
        if _parse_role(msg) == "assistant":
            text = _get_text_content(msg)
            if len(text) > 50:
                return text[:2000]
    return ""


def _strip_tool_mentions(text: str) -> str:
    """Remove tool result blocks that bloat the summary."""
    # remove ``` blocks
    text = re.sub(r"```[\s\S]*?```", "", text)
    # remove [tool: ...] lines
    text = re.sub(r"\[tool:.*?\]", "", text)
    return text.strip()


def _extract_files_changed(messages: list[dict[str, Any]], parts: list[dict[str, Any]]) -> list[str]:
    """Extract file paths from tool calls in parts.

    Looks for write/edit tool calls and bash commands with file paths.
    """
    files: set[str] = set()

    for part in parts:
        d = part.get("data", {})
        if not isinstance(d, dict):
            continue

        ptype = d.get("type", "")
        if ptype == "tool":
            call_input = d.get("state", {}).get("input", {})
            if isinstance(call_input, dict):
                fp = call_input.get("filePath", "")
                if fp and isinstance(fp, str):
                    files.add(fp)
                # also check command in bash calls
                cmd = call_input.get("command", "")
                if cmd and isinstance(cmd, str):
                    for f in _extract_paths_from_bash(cmd):
                        files.add(f)

    # Also scan message text for file paths
    for msg in messages:
        text = _get_text_content(msg)
        for match in re.finditer(r'["\'](/[\w/.\-]+\.[a-zA-Z]+)["\']', text):
            files.add(match.group(1))

    return sorted(files)[:50]


def _get_part_text(part: dict[str, Any]) -> str:
    """Extract text content from a part."""
    d = part.get("data", {})
    if not isinstance(d, dict):
        return ""
    ptype = d.get("type", "")
    if ptype in ("text",):
        return d.get("text", "") or ""
    if ptype in ("reasoning",):
        return f"[reasoning: {d.get('text', '')[:200]}]"
    return ""


def _build_conversation_text(
    messages: list[dict[str, Any]],
    parts: list[dict[str, Any]],
    max_msgs: int = 30,
    max_chars: int = 8000,
) -> str:
    """Build condensed conversation text for LLM summarization.

    Groups parts by message_id, extracts text/reasoning parts,
    strips code blocks, truncates oldest if over max_msgs.
    """
    msg_parts: dict[str, list[dict[str, Any]]] = {}
    for p in parts:
        mid = p.get("message_id", "")
        if mid:
            msg_parts.setdefault(mid, []).append(p)

    lines: list[str] = []
    total_chars = 0

    for msg in messages:
        mid = msg.get("id", "")
        role = _parse_role(msg)
        if role not in ("user", "assistant"):
            continue

        text_parts = msg_parts.get(mid, [])
        texts: list[str] = []
        for p in text_parts:
            t = _get_part_text(p)
            if t:
                t = re.sub(r"```[\s\S]*?```", "[code block]", t)
                t = re.sub(r"<tool_result>[\s\S]*?</tool_result>", "[tool result]", t)
                t = re.sub(r"\s+", " ", t).strip()
                if len(t) > 500:
                    t = t[:500] + "..."
                texts.append(t)

        if not texts:
            continue

        combined = " | ".join(texts)
        line = f"{role.capitalize()}: {combined}"

        if total_chars + len(line) > max_chars:
            remaining = max_chars - total_chars
            if remaining > 100:
                lines.append(line[:remaining] + "...")
            break

        lines.append(line)
        total_chars += len(line)

        if len(lines) >= max_msgs:
            break

    return "\n\n".join(lines)


def summarize_discussion_llm(
    messages: list[dict[str, Any]],
    parts: list[dict[str, Any]],
    ollama_url: str = "http://localhost:11434",
    model: str = "gemma3:4b",
    max_msgs: int = 30,
) -> str | None:
    """Send session conversation to Ollama and get a discussion summary.

    Returns 3-5 sentence narrative or None on failure.
    """
    conversation = _build_conversation_text(messages, parts, max_msgs=max_msgs)
    if not conversation.strip():
        return None

    prompt = (
        "Summarize the following conversation between a user and AI coding assistant. "
        "Focus on: what the user wanted to achieve, what approach was taken, "
        "what files were changed or created, and what the final outcome was. "
        "Write 3-5 concise sentences. Skip technical boilerplate.\n\n"
        f"CONVERSATION:\n{conversation}\n\nSUMMARY:"
    )

    try:
        resp = httpx.post(
            f"{ollama_url}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.3,
                    "num_predict": 512,
                },
            },
            timeout=120.0,
        )
        resp.raise_for_status()
        data = resp.json()
        summary = (data.get("response") or "").strip()
        return summary if len(summary) > 20 else None
    except httpx.HTTPStatusError as e:
        logger.warning("Ollama HTTP error: %s - %s", e.response.status_code, e.response.text[:200])
        return None
    except httpx.RequestError as e:
        logger.warning("Ollama request failed: %s", e)
        return None
    except Exception as e:
        logger.exception("Ollama summarization error: %s", e)
        return None


def generate_session_title(
    messages: list[dict[str, Any]],
    parts: list[dict[str, Any]],
    ollama_url: str = "http://localhost:11434",
    model: str = "gemma3:4b",
    max_msgs: int = 10,
) -> str | None:
    conversation = _build_conversation_text(messages, parts, max_msgs=max_msgs, max_chars=4000)
    if not conversation.strip():
        return None

    prompt = (
        "Read the following conversation between a user and AI coding assistant. "
        "Generate a concise title (3-10 words) that describes what this session is about. "
        "Respond with ONLY the title, no quotes, no prefix, no explanation.\n\n"
        f"CONVERSATION:\n{conversation}\n\nTITLE:"
    )

    try:
        resp = httpx.post(
            f"{ollama_url}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.3,
                    "num_predict": 30,
                },
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()
        title = (data.get("response") or "").strip().strip('"\' \t\n\r')
        if 5 < len(title) < 100:
            return title
        return None
    except Exception:
        logger.exception("Title generation failed")
        return None


def _extract_paths_from_bash(cmd: str) -> list[str]:
    """Extract likely file paths from a bash command."""
    paths: set[str] = set()
    # match paths that look like source files
    for match in re.finditer(r'(?:^|\s)(\/[\w/.\-]+\.[a-zA-Z]+)', cmd):
        paths.add(match.group(1).strip())
    return sorted(paths)


def _extract_tools_used(parts: list[dict[str, Any]]) -> list[str]:
    """Extract unique tool names from parts."""
    tools: set[str] = set()
    for part in parts:
        d = part.get("data", {})
        if not isinstance(d, dict):
            continue
        if d.get("type") == "tool":
            tool = d.get("tool", "")
            if tool:
                tools.add(tool)
    return sorted(tools)


def _extract_decisions(assistant_messages: list[dict[str, Any]]) -> list[str]:
    """Extract decision-like statements from assistant messages.

    Looks for patterns like "I'll use", "Let's go with", "decided to", etc.
    """
    decision_patterns = [
        r"(?:I(?:'ll| will) use|Let's go with|Going with|Using|Opting for)\s+(\w+(?:\.\w+)*(?:\s+\w+){0,5})",
        r"(?:decided|chose|selected|picked)\s+(?:to\s+)?(?:use\s+)?(\w+(?:\s+\w+){0,5})",
        r"(?:recommend|suggest|propose)\s+(?:using\s+)?(\w+(?:\s+\w+){0,5})",
        r"(?:approach|solution|strategy|pattern):\s*(.+?)(?:\.|$)",
    ]

    decisions: list[str] = []
    seen: set[str] = set()

    for msg in assistant_messages:
        text = _get_text_content(msg)
        for pat in decision_patterns:
            for match in re.finditer(pat, text, re.IGNORECASE):
                candidate = match.group(1).strip().rstrip(".,!?")
                if candidate and len(candidate) > 5 and candidate not in seen:
                    seen.add(candidate)
                    decisions.append(candidate)

    return decisions[:20]


def summarize_session(
    session: dict[str, Any],
    messages: list[dict[str, Any]],
    parts: list[dict[str, Any]],
    project_path: str | None = None,
) -> dict[str, Any]:
    """Produce a structured summary from raw session data.

    Returns a dict matching session_summary columns.
    """
    user_msgs = [m for m in messages if _parse_role(m) == "user"]
    assistant_msgs = [m for m in messages if _parse_role(m) == "assistant"]

    goal = _get_first_user_message(messages)
    outcomes = _get_last_assistant_message(messages)
    decisions = _extract_decisions(assistant_msgs)
    files = _extract_files_changed(messages, parts)
    tools = _extract_tools_used(parts)

    # Build a concise summary text
    summary_parts: list[str] = []
    agent = session.get("agent") or "?"
    title = session.get("title") or "Untitled"
    msg_count = len(messages)
    user_count = len(user_msgs)
    asst_count = len(assistant_msgs)

    summary_parts.append(f"[{agent}] {title}")
    summary_parts.append(f"Messages: {msg_count} ({user_count} user, {asst_count} assistant)")
    if tools:
        summary_parts.append(f"Tools: {', '.join(tools[:8])}")
    if files:
        summary_parts.append(f"Files: {len(files)} changed")
    summary_text = " | ".join(summary_parts)

    return {
        "session_id": session["id"],
        "title": session.get("title", "") or "",
        "slug": session.get("slug", "") or "",
        "agent": session.get("agent") or "",
        "model": session.get("model") or "",
        "project_id": session.get("project_id") or "",
        "project_path": project_path or "",
        "parent_id": session.get("parent_id"),
        "time_created": session.get("time_created", 0) or 0,
        "time_updated": session.get("time_updated", 0) or 0,
        "tokens_input": session.get("tokens_input", 0) or 0,
        "tokens_output": session.get("tokens_output", 0) or 0,
        "tokens_total": (session.get("tokens_input", 0) or 0) + (session.get("tokens_output", 0) or 0),
        "cost": session.get("cost", 0.0) or 0.0,
        "user_message_count": user_count,
        "assistant_message_count": asst_count,
        "goal": goal[:500] if goal else "",
        "outcomes": outcomes[:500] if outcomes else "",
        "decisions": json.dumps(decisions),
        "files_changed": json.dumps(files),
        "tools_used": json.dumps(tools),
        "summary_text": summary_text,
        "summary_version": 1,
        "discussion_summary": "",
        "discussion_summary_version": 0,
    }
