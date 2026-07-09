# Release Process

## Overview

Releases are a hybrid of CI automation and local AI-assisted note generation.

```
Push version bump ──► CI (lint, build, draft release)
                          │
                          ▼ (draft)
Run locally ──► AI release notes ──► Review ──► Publish
```

## Making a Release

### 1. Bump version

Edit `pyproject.toml`:

```toml
version = "0.2.0"
```

Commit and push to `main`:

```bash
git add -A && git commit -m "chore: bump to v0.2.0"
git push
```

This triggers the **Release** workflow:
- Runs `ruff check` and `uv build`
- Creates a **draft** GitHub Release with version as tag and raw commit log as placeholder notes

### 2. Generate AI release notes (local)

```bash
make release-notes
```

This runs `scripts/release_notes.py` which:
1. Reads git log and diff since the last tag
2. Sends context to local **Ollama** (`gemma3:4b`)
3. Categorizes changes into: **What's New**, **Fixes**, **Maintenance**
4. Writes `RELEASE_NOTES.md`

Requires Ollama running locally (`ollama serve`).

### 3. Review and edit

```bash
$EDITOR RELEASE_NOTES.md
```

Fix any mis-categorizations, add migration notes, or clarify user-facing descriptions.

### 4. Publish

```bash
make release-publish
```

This fills the draft release with your finalized notes, then publishes it.

## How it works

### CI workflow (`.github/workflows/release.yml`)

- **Trigger**: Push to `main` with changes to `pyproject.toml`, or manual `workflow_dispatch`
- **Quality gate**: `ruff check app/`
- **Build**: `uv build` — produces `.whl` and `.tar.gz`
- **Release**: Creates a **draft** release on GitHub (never auto-publishes)

### AI release notes (`scripts/release_notes.py`)

- Uses Ollama locally, no API keys, no external services
- Model: `gemma3:4b` (already pulled for this project)
- Temperature: 0.3 (consistent output, low creativity)
- Prompt includes: commit log, diff stat, code diff (truncated to 8k chars)
- Reads version from `pyproject.toml`, previous tag via `git describe`

### Version source of truth

`pyproject.toml` — single source. `app/__init__.py` reads it at runtime via `importlib.metadata`.

## First-time setup

Requirements already installed:
- `gh` CLI (authenticated)
- `ollama` (v0.24.0, gemma3:4b model pulled)
- `uv` (v0.6.2)

No additional setup needed for the release pipeline.
