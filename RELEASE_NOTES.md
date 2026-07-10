# v0.2.0

## Performance

* Dashboard page load queries reduced from ~11 to ~1-2 uncached queries per request.
* Aggregation-heavy endpoints (`stats`, `agents`, `models`, `projects`, `digests`) now cached in memory for 30-60s.
* `get_models()` replaced Python-side JSON loop (817 `json.loads()` calls) with single SQL `json_extract` query.
* Project detail page: `rglob("*.md")` no longer crawls entire project tree — skips `.venv`, `.git`, `node_modules`, `__pycache__` and 10+ other non-source directories, scans only first-level subdirs, caps at 50 files. This fixes extreme slowness on projects under `/home/harozien` (8.8GB).
* Project detail page: `get_project_stats()` now cached per-project (30s TTL). Model parsing uses SQL `json_extract` instead of Python loop.
* Project detail page: session list limit reduced from 200 to 100. Redundant `count_summaries()` query removed.
* Redundant `count_summarities()` query removed from dashboard — reuses count from `get_stats()`.
* Cache auto-invalidates after each extraction run, so the dashboard never shows stale data.

## Fixes

* CI release workflow: added `setup-uv` to `release` job (was missing, causing `uv build` to fail with exit code 127).
* CI release workflow: added `ruff` as dev dependency so quality gate passes in CI.
* Resolved 5 pre-existing ruff lint errors (unused imports, unused variable).

## Maintenance

* `app/db.py`: Added in-memory cache layer (`_cache` dict + `_cached()` + `invalidate_cache()`).
* `app/main.py`: Extracted `_scan_md_files()` helper with exclusion set for markdown scanning.
* Moved `import json` from local scope to module level in `app/db.py`.

# v0.1.0

## What's New
* Users can now view a list of markdown files within each project detail view.
* Sessions are now sorted by recent activity by default, and this preference is saved in your browser's local storage.
* The application automatically generates titles for sessions with placeholder or meaningless titles.
* Each session now includes a cost estimate, allowing you to sort sessions by cost and recency – with the most recent sessions appearing at the top.
* Images are now organized within the `docs/` directory.

## Fixes
* The application now correctly resolves the `project_path` from the session directory, ensuring accurate global session retrieval.

## Maintenance
* The project configuration files (pyproject.toml and uv.lock) have been updated.
* The build process now uses a lifecycle image for consistency.
* The application architecture has been reorganized for improved maintainability.
* Unnecessary files (ROADMAP.yaml and the link to it in the README) have been removed.
