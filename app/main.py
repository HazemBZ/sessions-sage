"""FastAPI app for Sessions-Sage dashboard."""

from __future__ import annotations

import json
import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.background import BackgroundScheduler

from app.config import CONFIG
from app.db import SummaryDB
from app.extractor import OpenCodeExtractor
from app.scheduler import run_discussion_summaries, run_extraction, run_initial_import, run_title_backfill
from app.summarizer import generate_session_title, is_meaningless_title, summarize_discussion_llm

logger = logging.getLogger(__name__)

# --- Globals ---
summary_db: SummaryDB
extractor: OpenCodeExtractor | None = None
scheduler: BackgroundScheduler | None = None

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global summary_db, extractor, scheduler

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    # Init DB
    summary_db = SummaryDB(CONFIG.summaries_db)
    summary_db.initialize()

    # Schedule extraction
    scheduler = BackgroundScheduler()

    def _run_extraction():
        global extractor
        if not CONFIG.opencode_db_exists:
            logger.warning("opencode.db not found at %s", CONFIG.opencode_db)
            return
        try:
            if extractor is None:
                extractor = OpenCodeExtractor(CONFIG.opencode_db)
            summary_db.initialize()
            count = summary_db.count_summaries()
            if count == 0:
                run_initial_import(extractor, summary_db)
            else:
                run_extraction(extractor, summary_db)
        except Exception:
            logger.exception("Extraction failed")

    # Run initial import on startup (in background)
    scheduler.add_job(_run_extraction, "interval", minutes=CONFIG.poll_interval_minutes, id="extract")

    def _run_discussion():
        if extractor is None:
            return
        try:
            run_discussion_summaries(extractor, summary_db)
        except Exception:
            logger.exception("Discussion summary job failed")

    scheduler.add_job(_run_discussion, "interval", minutes=2, id="discuss")

    def _run_title_backfill():
        if extractor is None:
            return
        try:
            run_title_backfill(extractor, summary_db)
        except Exception:
            logger.exception("Title backfill failed")

    from datetime import datetime, timezone, timedelta
    scheduler.add_job(
        _run_title_backfill, "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=90),
        id="title_backfill",
    )

    scheduler.start()

    # Also run immediately
    try:
        _run_extraction()
    except Exception:
        logger.exception("Initial extraction failed")

    # Start discussion summaries after a short delay (give extraction priority)
    import threading
    threading.Timer(30.0, _run_discussion).start()

    yield

    if scheduler:
        scheduler.shutdown(wait=False)
    if extractor:
        extractor.close()


app = FastAPI(title="Sessions-Sage", lifespan=lifespan)

# Templates
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Static
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---- Helper ----
def _fmt_ts(ms: int) -> str:
    if not ms:
        return ""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _fmt_date(ms: int) -> str:
    if not ms:
        return ""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d")


def _fmt_duration(ms_start: int, ms_end: int) -> str:
    if not ms_start or not ms_end:
        return ""
    secs = (ms_end - ms_start) // 1000
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    secs = secs % 60
    return f"{mins}m {secs}s" if secs else f"{mins}m"


def _parse_json_list(val: Any) -> list[str]:
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val) if val else []
        except (json.JSONDecodeError, TypeError):
            return []
    return []


# ---- Routes ----

@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    days: int = Query(default=7, ge=1, le=90),
    search: str = Query(default=""),
    project_id: str = Query(default=""),
    agent: str = Query(default=""),
    model: str = Query(default=""),
    discussion: str = Query(default=""),
):
    stats = summary_db.get_stats()

    sessions = summary_db.get_summaries(
        limit=100,
        days=days,
        search=search or None,
        project_id=project_id or None,
        agent=agent or None,
        model=model or None,
    )

    if discussion == "done":
        sessions = [s for s in sessions if s.get("discussion_summary")]
    elif discussion == "pending":
        sessions = [s for s in sessions if not s.get("discussion_summary")]

    # Parse JSON fields for template
    for s in sessions:
        s["decisions_list"] = _parse_json_list(s.get("decisions"))
        s["files_changed_list"] = _parse_json_list(s.get("files_changed"))
        s["tools_used_list"] = _parse_json_list(s.get("tools_used"))
        s["date"] = _fmt_date(s.get("time_created", 0))
        s["duration"] = _fmt_duration(s.get("time_created", 0), s.get("time_updated", 0))
        s["time_created_fmt"] = _fmt_ts(s.get("time_created", 0))
        s["time_updated_fmt"] = _fmt_ts(s.get("time_updated", 0))
        s["tokens_total"] = (s.get("tokens_input", 0) or 0) + (s.get("tokens_output", 0) or 0)
        s["has_discussion"] = bool(s.get("discussion_summary"))
        # Parse model JSON -> readable model name
        raw_model = s.get("model", "")
        if raw_model:
            try:
                parsed = json.loads(raw_model) if isinstance(raw_model, str) else raw_model
                s["model_name"] = parsed.get("id", raw_model)
                s["model_provider"] = parsed.get("providerID", "")
            except (json.JSONDecodeError, TypeError):
                s["model_name"] = raw_model
                s["model_provider"] = ""
        else:
            s["model_name"] = ""
            s["model_provider"] = ""

    agents = summary_db.get_agents()
    models = summary_db.get_models()
    projects = summary_db.get_projects()
    digests = summary_db.get_daily_digests(limit=14)

    for d in digests:
        d["projects_list"] = _parse_json_list(d.get("projects"))

    return templates.TemplateResponse(request, "index.html", {
        "stats": stats,
        "sessions": sessions,
        "agents": agents,
        "models": models,
        "projects": projects,
        "digests": digests,
        "days": days,
        "search": search,
        "selected_project": project_id,
        "selected_agent": agent,
        "selected_model": model,
        "total_sessions": stats.get("total_sessions", 0),
        "selected_discussion": discussion,
    })


@app.get("/session/{session_id}", response_class=HTMLResponse)
async def session_detail(request: Request, session_id: str):
    summary = summary_db.get_summary(session_id)
    if not summary:
        return HTMLResponse("Session not found", status_code=404)

    # Fetch raw data if available
    raw_session = None
    raw_messages = None
    raw_parts = None
    raw_parts_by_msg: dict[str, list[dict[str, Any]]] = {}
    if extractor:
        try:
            raw_session = extractor.get_session(session_id)
            raw_messages = extractor.get_messages(session_id)
            raw_parts = extractor.get_parts(session_id)
            for p in raw_parts or []:
                pid = p.get("message_id", "")
                if pid:
                    raw_parts_by_msg.setdefault(pid, []).append(p)
        except Exception:
            pass

    notes = summary_db.get_notes(session_id)

    # Parse JSON fields
    summary["decisions_list"] = _parse_json_list(summary.get("decisions"))
    summary["files_changed_list"] = _parse_json_list(summary.get("files_changed"))
    summary["tools_used_list"] = _parse_json_list(summary.get("tools_used"))
    summary["time_created_fmt"] = _fmt_ts(summary.get("time_created", 0))
    summary["time_updated_fmt"] = _fmt_ts(summary.get("time_updated", 0))
    summary["duration"] = _fmt_duration(summary.get("time_created", 0), summary.get("time_updated", 0))
    summary["date"] = _fmt_date(summary.get("time_created", 0))

    # Child sessions
    child_summaries = []
    sid = summary.get("session_id", "")
    if sid:
        child_summaries = summary_db.get_summaries(parent_id=sid)

    return templates.TemplateResponse(request, "session.html", {
        "summary": summary,
        "raw_session": raw_session,
        "raw_messages": raw_messages,
        "raw_parts": raw_parts,
        "raw_parts_by_msg": raw_parts_by_msg,
        "notes": notes,
        "child_summaries": child_summaries,
        "discussion_summary_enabled": True,
    })


@app.post("/session/{session_id}/note", response_class=HTMLResponse)
async def add_note(request: Request, session_id: str, note: str = Form(...)):
    if note.strip():
        summary_db.add_note(session_id, note.strip())
    # Redirect back to session page
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/session/{session_id}", status_code=303)


@app.post("/session/{session_id}/regenerate-summary", response_class=HTMLResponse)
async def regenerate_summary(request: Request, session_id: str):
    """On-demand regenerate discussion summary for a session."""
    global extractor

    if not extractor:
        return HTMLResponse("Extractor not available", status_code=503)

    summary = summary_db.get_summary(session_id)
    if not summary:
        return HTMLResponse("Session not found", status_code=404)

    try:
        messages = extractor.get_messages(session_id)
        parts = extractor.get_parts(session_id)

        result = summarize_discussion_llm(
            messages, parts,
            ollama_url=CONFIG.ollama_url,
            model=CONFIG.ollama_model,
            max_msgs=CONFIG.discussion_max_messages,
        )

        if result:
            summary_db.update_discussion_summary(
                session_id, result, CONFIG.discussion_summary_version,
            )

            current_title = summary.get("title", "")
            if is_meaningless_title(current_title):
                new_title = generate_session_title(
                    messages, parts,
                    ollama_url=CONFIG.ollama_url,
                    model=CONFIG.ollama_model,
                )
                if new_title:
                    summary_db.update_session_title(session_id, new_title)
        else:
            summary_db.update_discussion_summary(session_id, "", 0)

    except Exception:
        logger.exception("Failed to regenerate summary for %s", session_id)

    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/session/{session_id}", status_code=303)


@app.get("/digests", response_class=HTMLResponse)
async def digests_view(request: Request):
    digests = summary_db.get_daily_digests(limit=60)
    for d in digests:
        d["projects_list"] = _parse_json_list(d.get("projects"))

    return templates.TemplateResponse(request, "digests.html", {
        "digests": digests,
    })


@app.get("/projects", response_class=HTMLResponse)
async def projects_list(
    request: Request,
    sort: str = Query(default="recent"),
):
    projects = summary_db.get_projects()
    # Enrich with per-project stats
    enriched = []
    for pid, ppath, count in projects:
        pstats = summary_db.get_project_stats(pid)
        last_session_ts = pstats.get("last_session", 0)
        enriched.append({
            "project_id": pid,
            "project_path": ppath,
            "session_count": count,
            "discussion_done": pstats.get("discussion_done", 0),
            "first_session": _fmt_ts(pstats.get("first_session", 0)),
            "last_session": _fmt_ts(last_session_ts),
            "last_session_ts": last_session_ts,
            "total_cost": pstats.get("total_cost", 0),
            "total_tokens_input": pstats.get("total_tokens_input", 0),
            "total_tokens_output": pstats.get("total_tokens_output", 0),
        })

    # Sort
    if sort == "cost":
        enriched.sort(key=lambda p: -p["total_cost"])
    elif sort == "sessions":
        enriched.sort(key=lambda p: -p["session_count"])
    else:  # recent (default)
        enriched.sort(key=lambda p: -(p.get("last_session_ts") or 0))

    total_cost = sum(p["total_cost"] for p in enriched)
    total_tokens_in = sum(p["total_tokens_input"] for p in enriched)
    total_tokens_out = sum(p["total_tokens_output"] for p in enriched)

    return templates.TemplateResponse(request, "projects.html", {
        "projects": enriched,
        "total_cost": total_cost,
        "total_tokens_in": total_tokens_in,
        "total_tokens_out": total_tokens_out,
        "total_sessions": summary_db.count_summaries(),
        "selected_sort": sort,
    })


_EXCLUDED_MD_DIRS = frozenset({
    ".venv", ".git", "node_modules", "__pycache__", ".codegraph",
    ".omo", ".cache", "venv", "dist", "build", ".egg-info",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
})


def _scan_md_files(project_path: str, max_files: int = 50) -> list[str]:
    """Scan project for markdown files, skipping common non-source dirs.

    Limits to `max_files` to avoid crawling massive directory trees
    (e.g. /home/harozien/.venv).
    """
    proj_dir = Path(project_path)
    if not proj_dir.exists():
        return []
    result: list[str] = []
    try:
        for entry in proj_dir.iterdir():
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if entry.name in _EXCLUDED_MD_DIRS:
                continue
            for f in sorted(entry.rglob("*.md")):
                if f.is_file() and f.parent.name not in _EXCLUDED_MD_DIRS:
                    try:
                        result.append(str(f.relative_to(proj_dir)))
                    except ValueError:
                        pass
                    if len(result) >= max_files:
                        return result
    except PermissionError:
        pass
    return result


@app.get("/project/{project_id:path}", response_class=HTMLResponse)
async def project_detail(
    request: Request,
    project_id: str,
    sort: str = Query(default="recent"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=10, le=200),
):
    offset = (page - 1) * per_page
    sessions = summary_db.get_project_sessions(project_id, limit=per_page, offset=offset, sort=sort)
    if not sessions and page > 1:
        # Page beyond available data — redirect to page 1
        return RedirectResponse(f"/project/{project_id}?sort={sort}", status_code=302)

    pstats = summary_db.get_project_stats(project_id)
    project_name = sessions[0].get("project_path", project_id) if sessions else project_id
    project_short = project_name.split("/")[-1] if project_name else project_id

    for s in sessions:
        s["decisions_list"] = _parse_json_list(s.get("decisions"))
        s["files_changed_list"] = _parse_json_list(s.get("files_changed"))
        s["tools_used_list"] = _parse_json_list(s.get("tools_used"))
        s["time_created_fmt"] = _fmt_ts(s.get("time_created", 0))
        s["time_updated_fmt"] = _fmt_ts(s.get("time_updated", 0))
        s["duration"] = _fmt_duration(s.get("time_created", 0), s.get("time_updated", 0))
        s["date"] = _fmt_date(s.get("time_created", 0))
        s["has_discussion"] = bool(s.get("discussion_summary"))
        raw_model = s.get("model", "")
        if raw_model:
            try:
                parsed = json.loads(raw_model) if isinstance(raw_model, str) else raw_model
                s["model_name"] = parsed.get("id", raw_model)
            except (json.JSONDecodeError, TypeError):
                s["model_name"] = raw_model
        else:
            s["model_name"] = ""

    md_files = _scan_md_files(project_name) if project_name else []

    total = pstats.get("total_sessions", 0)
    total_pages = max(1, (total + per_page - 1) // per_page) if total else 1
    # Page range for pagination controls (show ~5 pages around current)
    window = 2
    range_start = max(1, page - window)
    range_end = min(total_pages, page + window)
    if range_end - range_start < window * 2:
        if range_start == 1:
            range_end = min(total_pages, range_start + window * 2)
        else:
            range_start = max(1, range_end - window * 2)
    page_range = list(range(range_start, range_end + 1))

    return templates.TemplateResponse(request, "project.html", {
        "sessions": sessions,
        "project_name": project_name,
        "project_short": project_short,
        "project_path": project_name,
        "project_id": project_id,
        "selected_sort": sort,
        "stats": {
            **pstats,
            "first_session_fmt": _fmt_ts(pstats.get("first_session", 0)),
            "last_session_fmt": _fmt_ts(pstats.get("last_session", 0)),
        },
        "md_files": md_files,
        "total_sessions": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "page_range": page_range,
        "prev_page": page - 1 if page > 1 else None,
        "next_page": page + 1 if page < total_pages else None,
    })


@app.get("/api/stats")
async def api_stats():
    return summary_db.get_stats()


@app.get("/api/sessions")
async def api_sessions(
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0),
    days: int = Query(default=0, ge=0),
    search: str = Query(default=""),
):
    sessions = summary_db.get_summaries(
        limit=limit,
        offset=offset,
        days=days or None,
        search=search or None,
    )
    return {"sessions": sessions, "total": summary_db.count_summaries()}


@app.get("/api/sessions/{session_id}/messages")
async def api_session_messages(session_id: str):
    """Return raw session messages + parts as JSON."""
    if not extractor:
        return {"error": "Extractor not available"}, 503

    session = extractor.get_session(session_id)
    if not session:
        return {"error": "Session not found"}, 404

    messages = extractor.get_messages(session_id)
    parts = extractor.get_parts(session_id)

    parts_by_msg: dict[str, list[dict[str, Any]]] = {}
    for p in parts:
        pid = p.get("message_id", "")
        if pid:
            parts_by_msg.setdefault(pid, []).append(p)

    enriched: list[dict[str, Any]] = []
    for m in messages:
        mid = m.get("id", "")
        msg_parts = parts_by_msg.get(mid, [])

        data = m.get("data", {})
        role = data.get("role", "unknown") if isinstance(data, dict) else "unknown"

        part_summaries = []
        for p in msg_parts:
            pd = p.get("data", {})
            ptype = pd.get("type", "") if isinstance(pd, dict) else ""
            ptext = ""
            if isinstance(pd, dict):
                if ptype == "text":
                    ptext = pd.get("text", "")
                elif ptype == "reasoning":
                    ptext = pd.get("text", "")[:500]
                elif ptype == "tool":
                    tool_name = pd.get("tool", "")
                    tool_input = pd.get("state", {}).get("input", {})
                    ptext = f"[{tool_name}] {json.dumps(tool_input, default=str)[:500]}" if tool_input else f"[{tool_name}]"
            part_summaries.append({
                "id": p.get("id", ""),
                "message_id": mid,
                "type": ptype,
                "text": ptext,
                "data": pd,
            })

        enriched.append({
            "id": mid,
            "session_id": m.get("session_id", ""),
            "time_created": m.get("time_created", 0),
            "role": role,
            "part_count": len(msg_parts),
            "parts": part_summaries,
            "data": data,
        })

    return {
        "session_id": session_id,
        "session": session,
        "message_count": len(enriched),
        "part_count": len(parts),
        "messages": enriched,
    }


# ---- CLI entry point ----
def run() -> None:
    """Run the server."""
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=CONFIG.host,
        port=CONFIG.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    run()
