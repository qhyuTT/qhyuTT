#!/usr/bin/env python3
"""Refresh generated sections in the profile README."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
USERNAME = os.getenv("GITHUB_USERNAME") or os.getenv("GITHUB_REPOSITORY_OWNER") or "qhyuTT"
TOKEN = os.getenv("GITHUB_TOKEN")
START = "<!-- RECENT-REPOS:START -->"
END = "<!-- RECENT-REPOS:END -->"
MAX_REPOS = 6


def request_json(url: str) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{USERNAME}-profile-readme-updater",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"

    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API request failed: {exc.code} {detail}") from exc


def escape_table_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def compact_description(repo: dict[str, Any]) -> str:
    description = repo.get("description") or "Personal notes and experiments."
    description = re.sub(r"\s+", " ", description).strip()
    if len(description) <= 84:
        return description
    return description[:81].rstrip() + "..."


def format_date(value: str | None) -> str:
    if not value:
        return "-"
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.strftime("%Y-%m-%d")


def load_repos() -> list[dict[str, Any]]:
    url = (
        f"https://api.github.com/users/{USERNAME}/repos"
        "?per_page=100&sort=updated&direction=desc"
    )
    repos = request_json(url)
    if not isinstance(repos, list):
        raise RuntimeError("Unexpected GitHub API response for repositories.")

    visible = [
        repo
        for repo in repos
        if not repo.get("archived") and repo.get("name") != USERNAME
    ]
    own_repos = [repo for repo in visible if not repo.get("fork")]
    selected = own_repos[:MAX_REPOS]
    if len(selected) < MAX_REPOS:
        selected += [repo for repo in visible if repo not in selected][: MAX_REPOS - len(selected)]
    return selected[:MAX_REPOS]


def render_recent_repos(repos: list[dict[str, Any]]) -> str:
    rows = [
        "| Repository | Description | Stack | Updated |",
        "| --- | --- | --- | --- |",
    ]
    for repo in repos:
        name = escape_table_cell(repo["name"])
        url = repo["html_url"]
        description = escape_table_cell(compact_description(repo))
        language = repo.get("language") or "Mixed"
        if repo.get("fork"):
            language = f"Fork / {language}"
        updated = format_date(repo.get("updated_at") or repo.get("pushed_at"))
        rows.append(f"| [{name}]({url}) | {description} | {language} | {updated} |")
    return "\n".join(rows)


def update_readme(section: str) -> None:
    readme = README.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"{re.escape(START)}\n.*?\n{re.escape(END)}",
        flags=re.DOTALL,
    )
    replacement = f"{START}\n{section}\n{END}"
    updated, count = pattern.subn(replacement, readme)
    if count != 1:
        raise RuntimeError("README markers were not found exactly once.")
    README.write_text(updated, encoding="utf-8")


def main() -> int:
    repos = load_repos()
    update_readme(render_recent_repos(repos))
    return 0


if __name__ == "__main__":
    sys.exit(main())
