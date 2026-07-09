#!/usr/bin/env python3
"""Generate release notes via local Ollama.

Usage:
    uv run python scripts/release_notes.py [previous_tag] [version]

If no args given, reads previous tag via `git describe` and version from pyproject.toml.
Writes RELEASE_NOTES.md, prints result to stdout.
"""

import subprocess
import sys
import httpx
import re
import os

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "gemma3:4b"


def sh(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_DIR).stdout.strip()


def get_previous_tag() -> str:
    tag = sh(["git", "describe", "--tags", "--abbrev=0", "HEAD^"])
    if not tag:
        # try current HEAD too
        tag = sh(["git", "describe", "--tags", "--abbrev=0"])
    if not tag:
        # check if there are any tags at all
        all_tags = sh(["git", "tag", "--list", "--sort=-version:refname"])
        if all_tags:
            tag = all_tags.split("\n")[0]
    return tag or "none"


def get_version() -> str:
    match = re.search(r'^version = "(.*)"', open(os.path.join(PROJECT_DIR, "pyproject.toml")).read(), re.M)
    return match.group(1) if match else "unknown"


def gather_context(prev_tag: str) -> dict:
    if prev_tag and prev_tag != "none":
        git_log = sh(["git", "log", "--oneline", f"{prev_tag}..HEAD"])
        diffstat = sh(["git", "diff", "--stat", f"{prev_tag}..HEAD"])
        raw_diff = sh(["git", "diff", f"{prev_tag}..HEAD"])
    else:
        git_log = sh(["git", "log", "--oneline", "HEAD"])
        diffstat = sh(["git", "diff", "--stat", "4b825dc642cb6eb9a060e54bf899d153036d2e0e", "HEAD"])
        raw_diff = sh(["git", "diff", "4b825dc642cb6eb9a060e54bf899d153036d2e0e", "HEAD"])

    return {"log": git_log, "diffstat": diffstat, "diff": raw_diff[:8000]}  # cap diff to avoid huge prompts


def check_ollama() -> bool:
    try:
        r = httpx.get("http://localhost:11434/api/tags", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def pull_model() -> bool:
    try:
        r = httpx.post("http://localhost:11434/api/pull", json={"name": MODEL, "stream": False}, timeout=300)
        return r.status_code == 200
    except Exception:
        return False


def generate_notes(version: str, ctx: dict) -> str:
    prompt = f"""You are writing release notes for a software project called "sessions-sage" — an OpenCode session summarizer and reflection dashboard (Python + FastAPI).

Generate release notes for version {version}.

Recent commits:
{ctx['log']}

Files changed:
{ctx['diffstat']}

Code diff (truncated):
{ctx['diff']}

Write release notes in this format:

## What's New
(Bullet list of new features and improvements in present tense, user-friendly language. Group related items.)

## Fixes
(Bullet list of bug fixes. Omit if none.)

## Maintenance
(Bullet list of refactors, dependency updates, tooling changes. Omit if none.)

Keep descriptions brief but informative. Focus on what the user notices. Do NOT use markdown heading syntax inside list items."""

    resp = httpx.post(OLLAMA_URL, json={
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": 1024},
    }, timeout=120)

    if resp.status_code != 200:
        raise RuntimeError(f"Ollama error {resp.status_code}: {resp.text}")

    raw = resp.json()["response"].strip()

    return f"# v{version}\n\n{raw}\n"


def main():
    prev_tag = sys.argv[1] if len(sys.argv) > 1 else get_previous_tag()
    version = sys.argv[2] if len(sys.argv) > 2 else get_version()

    print(f"🤖 Generating release notes for v{version}")
    print(f"📎 Previous tag: {prev_tag}")

    if not check_ollama():
        print("❌ Ollama not running. Start with: ollama serve")
        sys.exit(1)

    ctx = gather_context(prev_tag)
    commit_count = len([line for line in ctx["log"].split("\n") if line.strip()])
    print(f"📝 {commit_count} commits since {prev_tag}")

    notes = generate_notes(version, ctx)

    path = os.path.join(PROJECT_DIR, "RELEASE_NOTES.md")
    with open(path, "w") as f:
        f.write(notes)

    print(f"\n✅ Wrote {path}\n")
    print("─" * 50)
    print(notes)
    print("─" * 50)
    print("\n📋 Review and edit the notes, then publish:")
    print("   $EDITOR RELEASE_NOTES.md")
    print(f"   make release-publish  # or: gh release edit v{version} --notes-file RELEASE_NOTES.md --draft=false")


if __name__ == "__main__":
    main()
