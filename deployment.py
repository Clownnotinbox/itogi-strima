"""Publish the static site locally and optionally push it to GitHub Pages."""
from __future__ import annotations

import subprocess
from pathlib import Path

import siteutil
from config import BASE_DIR, PAGES_AUTO_PUSH, PAGES_BRANCH, PAGES_REPO_URL, PUBLIC_DIR


def _run(args: list[str], cwd: Path) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=120,
        )
    except Exception as e:
        return False, str(e)
    out = "\n".join(x for x in (proc.stdout.strip(), proc.stderr.strip()) if x)
    return proc.returncode == 0, out


def _git_ready() -> tuple[bool, str]:
    if not (BASE_DIR / ".git").exists():
        if not PAGES_REPO_URL:
            return False, "Git is not initialized and KRYL_PAGES_REPO_URL is empty"
        ok, out = _run(["git", "init", "-b", PAGES_BRANCH], BASE_DIR)
        if not ok:
            return False, out
        ok, out = _run(["git", "remote", "add", "origin", PAGES_REPO_URL], BASE_DIR)
        if not ok:
            return False, out
    ok, out = _run(["git", "remote", "get-url", "origin"], BASE_DIR)
    if not ok:
        if not PAGES_REPO_URL:
            return False, "Git remote 'origin' is not configured. Run setup_pages_repo.bat with your GitHub repo URL."
        ok, out = _run(["git", "remote", "add", "origin", PAGES_REPO_URL], BASE_DIR)
        if not ok:
            return False, out
    ok, branch = _run(["git", "branch", "--show-current"], BASE_DIR)
    if not ok:
        return False, branch
    if branch.strip() != PAGES_BRANCH:
        return False, f"Current branch is '{branch.strip()}'. Switch to '{PAGES_BRANCH}' before auto-push."
    return True, "ready"


def publish_local() -> dict:
    return siteutil.publish()


def publish_and_maybe_push(message: str = "Update stream summary site") -> dict:
    payload = publish_local()
    result = {
        "ok": True,
        "stats": payload.get("stats"),
        "generated_at": payload.get("generated_at"),
        "pushed": False,
        "push_message": "auto-push disabled",
    }
    if not PAGES_AUTO_PUSH:
        return result

    ready, msg = _git_ready()
    if not ready:
        result.update({"ok": False, "push_message": msg})
        return result

    steps = [
        [
            "git",
            "add",
            "public/index.html",
            "public/live.css",
            "public/live.js",
            "public/data.json",
        ],
        ["git", "diff", "--cached", "--quiet"],
    ]
    ok, out = _run(steps[0], BASE_DIR)
    if not ok:
        result.update({"ok": False, "push_message": out})
        return result

    changed, _ = _run(steps[1], BASE_DIR)
    if changed:
        result["push_message"] = "no changes"
        return result

    ok, out = _run(["git", "commit", "-m", message], BASE_DIR)
    if not ok:
        result.update({"ok": False, "push_message": out})
        return result
    ok, out = _run(["git", "push", "-u", "origin", PAGES_BRANCH], BASE_DIR)
    result.update({"ok": ok, "pushed": ok, "push_message": out})
    return result
