"""Background scheduler for periodic session extraction and summarization."""

from __future__ import annotations

import logging
import time

from app.config import CONFIG
from app.db import SummaryDB
from app.extractor import OpenCodeExtractor

logger = logging.getLogger(__name__)


def run_extraction(extractor: OpenCodeExtractor, summary_db: SummaryDB) -> int:
    """Check for new/updated sessions and summarize them.

    Returns number of sessions processed.
    """
    last_checked, last_id = summary_db.get_cursor()
    now = int(time.time() * 1000)

    try:
        sessions = extractor.get_updated_sessions(since_epoch_ms=last_checked)
    except FileNotFoundError:
        logger.warning("opencode.db not found, skipping")
        return 0

    if not sessions:
        # update cursor to now so we don't re-check stale timestamp
        summary_db.update_cursor(now, last_id)
        return 0

    processed = 0
    max_session_id = last_id
    import json

    for ses in sessions:
        sid = ses["id"]
        try:
            messages = extractor.get_messages(sid)
            parts = extractor.get_parts(sid)

            # resolve project path from worktree column
            project_path = None
            pid = ses.get("project_id")
            if pid:
                proj = extractor.get_project(pid) if pid != "global" else {"worktree": "/"}
                if proj:
                    project_path = proj.get("worktree") or None

            from app.summarizer import summarize_session
            summary = summarize_session(ses, messages, parts, project_path)
            summary_db.upsert_summary(summary)

            if sid > max_session_id:
                max_session_id = sid
            processed += 1

        except Exception:
            logger.exception("Failed to process session %s", sid)

    summary_db.update_cursor(now, max_session_id)
    logger.info("Processed %d sessions (cursor: %s)", processed, max_session_id)

    # Rebuild daily digests
    try:
        summary_db.rebuild_daily_digests()
    except Exception:
        logger.exception("Failed to rebuild daily digests")

    return processed


def run_discussion_summaries(extractor: OpenCodeExtractor, summary_db: SummaryDB) -> int:
    """Process sessions missing LLM discussion summaries.

    Rate-limited to avoid overwhelming Ollama.
    Returns number of sessions summarized.
    """
    pending = summary_db.get_summaries_without_discussion_summary(
        limit=CONFIG.discussion_batch_size,
        target_version=CONFIG.discussion_summary_version,
    )
    if not pending:
        return 0

    from app.summarizer import summarize_discussion_llm

    processed = 0
    for row in pending:
        sid = row["session_id"]
        try:
            messages = extractor.get_messages(sid)
            parts = extractor.get_parts(sid)

            summary = summarize_discussion_llm(
                messages,
                parts,
                ollama_url=CONFIG.ollama_url,
                model=CONFIG.ollama_model,
                max_msgs=CONFIG.discussion_max_messages,
            )

            if summary:
                summary_db.update_discussion_summary(
                    sid, summary, CONFIG.discussion_summary_version,
                )
                logger.info("Discussion summary for %s (%d chars)", sid, len(summary))
                processed += 1
            else:
                # Mark as version=0 so we don't retry broken sessions endlessly
                summary_db.update_discussion_summary(sid, "", 0)

            time.sleep(CONFIG.discussion_rate_limit_s)

        except Exception:
            logger.exception("Failed to summarize session %s", sid)

    logger.info("Discussion summaries: %d processed (pending: %d)", processed, len(pending))
    return processed


def run_initial_import(extractor: OpenCodeExtractor, summary_db: SummaryDB) -> int:
    """Import sessions from opencode.db (used on first run).

    Processes newest sessions first, limits to a reasonable batch.
    Subsequent runs use the incremental cursor.
    """
    last_checked, _ = summary_db.get_cursor()
    if last_checked > 0:
        return run_extraction(extractor, summary_db)

    now = int(time.time() * 1000)
    processed = 0
    max_id = ""

    import json

    total_available = extractor.get_session_count()
    sessions = extractor.get_recent_sessions(limit=total_available)
    # reverse so newest processed last (cursor ends at newest)
    for ses in reversed(sessions):
        sid = ses["id"]
        try:
            # Skip sessions with no messages (shell sessions)
            msg_count = extractor.get_messages_count(sid)
            if msg_count == 0:
                continue

            messages = extractor.get_messages(sid)
            parts = extractor.get_parts(sid)

            project_path = None
            pid = ses.get("project_id")
            if pid:
                proj = extractor.get_project(pid) if pid != "global" else {"worktree": "/"}
                if proj:
                    project_path = proj.get("worktree") or None

            from app.summarizer import summarize_session
            summary = summarize_session(ses, messages, parts, project_path)
            summary_db.upsert_summary(summary)

            if sid > max_id:
                max_id = sid
            processed += 1

            if processed % 20 == 0:
                logger.info("Initial import progress: %d sessions", processed)

        except Exception:
            logger.exception("Failed to process session %s", sid)

    summary_db.update_cursor(now, max_id)
    summary_db.rebuild_daily_digests()
    logger.info("Initial import complete: %d sessions processed", processed)
    return processed
