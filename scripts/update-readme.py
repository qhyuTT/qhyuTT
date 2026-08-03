#!/usr/bin/env python3
"""Refresh the generated sections of the profile README.

Two regions are regenerated in place, between HTML comment markers:

  * Recently Updated table   (RECENT-REPOS markers)
  * Snapshot / Live Metrics  (METRICS markers)

The metrics block is rendered as plain markdown + a little inline HTML, with the
numbers pulled from the GitHub API on each run. Nothing is fetched by the
browser when the profile is viewed, so the section can never render as a broken
image the way third-party stat-card services do when they are rate limited or
down. The GitHub Action runs this daily to keep the numbers fresh.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
USERNAME = os.getenv("GITHUB_USERNAME") or os.getenv("GITHUB_REPOSITORY_OWNER") or "qhyuTT"
TOKEN = os.getenv("GITHUB_TOKEN")

RECENT_START = "<!-- RECENT-REPOS:START -->"
RECENT_END = "<!-- RECENT-REPOS:END -->"
METRICS_START = "<!-- METRICS:START -->"
METRICS_END = "<!-- METRICS:END -->"

MAX_REPOS = 6
TOP_LANGUAGES = 6
BAR_WIDTH = 22
SPARK_WEEKS = 30
SPARK_CHARS = "▁▂▃▄▅▆▇█"


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def _headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{USERNAME}-profile-readme-updater",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    return headers


def request_json(url: str) -> Any:
    request = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub REST request failed: {exc.code} {detail}") from exc


def graphql(query: str, variables: dict[str, Any]) -> Any:
    if not TOKEN:
        raise RuntimeError("GraphQL requires GITHUB_TOKEN")
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    headers = _headers()
    headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        "https://api.github.com/graphql", data=body, headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GraphQL request failed: {exc.code} {detail}") from exc
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    return payload["data"]


# --------------------------------------------------------------------------- #
# Recently Updated table (unchanged behaviour)
# --------------------------------------------------------------------------- #
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


def select_recent(repos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    visible = [
        repo
        for repo in repos
        if not repo.get("archived") and repo.get("name") != USERNAME
    ]
    own_repos = [repo for repo in visible if not repo.get("fork")]
    selected = own_repos[:MAX_REPOS]
    if len(selected) < MAX_REPOS:
        selected += [repo for repo in visible if repo not in selected][
            : MAX_REPOS - len(selected)
        ]
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


# --------------------------------------------------------------------------- #
# Metrics: profile, languages, stars, contributions
# --------------------------------------------------------------------------- #
def owned(repos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        repo
        for repo in repos
        if not repo.get("fork")
        and not repo.get("archived")
        and repo.get("name") != USERNAME
    ]


def language_bytes(repos: list[dict[str, Any]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for repo in repos:
        url = repo.get("languages_url")
        if not url:
            continue
        try:
            data = request_json(url)
        except RuntimeError as exc:
            print(f"  languages skipped for {repo.get('name')}: {exc}", file=sys.stderr)
            continue
        for language, count in data.items():
            totals[language] = totals.get(language, 0) + int(count)
    return totals


CONTRIB_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}
"""


def fetch_daily_contributions(created_at: str) -> dict[str, int]:
    """Return {ISO date: count} for every day from account creation to today."""
    start_year = int(created_at[:4])
    now = datetime.now(timezone.utc)
    daily: dict[str, int] = {}
    for year in range(start_year, now.year + 1):
        # GitHub rejects a window wider than one year or a `to` in the future,
        # so cap the current year at "now".
        end = (
            now.strftime("%Y-%m-%dT%H:%M:%SZ")
            if year == now.year
            else f"{year}-12-31T23:59:59Z"
        )
        variables = {
            "login": USERNAME,
            "from": f"{year}-01-01T00:00:00Z",
            "to": end,
        }
        data = graphql(CONTRIB_QUERY, variables)
        user = data.get("user") or {}
        calendar = (user.get("contributionsCollection") or {}).get(
            "contributionCalendar"
        ) or {}
        for week in calendar.get("weeks", []):
            for day in week.get("contributionDays", []):
                daily[day["date"]] = day["contributionCount"]
    return daily


def sparkline(values: list[int]) -> str:
    if not values:
        return ""
    peak = max(values) or 1
    out = []
    for value in values:
        index = int(round((len(SPARK_CHARS) - 1) * value / peak))
        out.append(SPARK_CHARS[index])
    return "".join(out)


def summarise_contributions(daily: dict[str, int]) -> dict[str, Any]:
    today = datetime.now(timezone.utc).date()
    total = sum(daily.values())
    this_year = sum(v for d, v in daily.items() if d[:4] == str(today.year))

    # Longest streak: longest run of consecutive calendar days with activity.
    longest = run = 0
    for iso in sorted(daily):
        run = run + 1 if daily[iso] > 0 else 0
        longest = max(longest, run)

    # Current streak: walk back from today (today may not have activity yet).
    cursor = today
    if daily.get(cursor.isoformat(), 0) == 0:
        cursor -= timedelta(days=1)
    current = 0
    while daily.get(cursor.isoformat(), 0) > 0:
        current += 1
        cursor -= timedelta(days=1)

    # Weekly sparkline for the trailing SPARK_WEEKS weeks.
    span = [today - timedelta(days=i) for i in range(SPARK_WEEKS * 7)][::-1]
    weekly = [
        sum(daily.get(day.isoformat(), 0) for day in span[w * 7 : (w + 1) * 7])
        for w in range(SPARK_WEEKS)
    ]

    return {
        "total": total,
        "this_year": this_year,
        "current": current,
        "longest": longest,
        "spark": sparkline(weekly),
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def stat_cell(value: Any, label: str) -> str:
    return (
        '    <td align="center">'
        f"<strong>{value}</strong><br /><sub>{label}</sub></td>"
    )


def render_language_bar(lang_bytes: dict[str, int]) -> str:
    total = sum(lang_bytes.values())
    if not total:
        return ""
    ranked = sorted(lang_bytes.items(), key=lambda item: item[1], reverse=True)
    rows = ranked[:TOP_LANGUAGES]
    other = total - sum(count for _, count in rows)
    if other > 0:
        rows.append(("Other", other))
    name_width = max(len(name) for name, _ in rows)
    lines = []
    for name, count in rows:
        pct = 100 * count / total
        filled = int(round(BAR_WIDTH * count / total))
        bar = "█" * filled + "░" * (BAR_WIDTH - filled)
        lines.append(f"{name.ljust(name_width)}  {bar}  {pct:5.1f}%")
    return "\n".join(lines)


def render_metrics(
    profile: dict[str, Any],
    stars: int,
    lang_bytes: dict[str, int],
    contrib: dict[str, Any] | None,
) -> str:
    cells = []
    if contrib:
        cells.append(stat_cell(contrib["total"], "Contributions"))
    cells.append(stat_cell(profile.get("public_repos", 0), "Public repos"))
    cells.append(stat_cell(stars, "Stars earned"))
    cells.append(stat_cell(profile.get("followers", 0), "Followers"))
    if contrib:
        cells.append(stat_cell(contrib["current"], "Current streak"))
        cells.append(stat_cell(contrib["longest"], "Longest streak"))

    parts = [
        '<div align="center">',
        "",
        "<table>",
        "  <tr>",
        *cells,
        "  </tr>",
        "</table>",
        "",
        "</div>",
    ]

    bar = render_language_bar(lang_bytes)
    if bar:
        parts += [
            "",
            "**Language mix** &nbsp;·&nbsp; bytes across public repositories",
            "",
            "```text",
            bar,
            "```",
        ]

    if contrib and contrib.get("spark"):
        parts += [
            "",
            f"**Contribution activity** &nbsp;·&nbsp; last {SPARK_WEEKS} weeks",
            "",
            "```text",
            contrib["spark"],
            "```",
            "",
            f"<sub>{contrib['this_year']} contributions this year &nbsp;·&nbsp; "
            f"longest streak {contrib['longest']} days &nbsp;·&nbsp; "
            "refreshed daily</sub>",
        ]

    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# README rewrite
# --------------------------------------------------------------------------- #
def replace_section(readme: str, start: str, end: str, section: str) -> str:
    pattern = re.compile(
        rf"{re.escape(start)}\n.*?\n{re.escape(end)}",
        flags=re.DOTALL,
    )
    replacement = f"{start}\n{section}\n{end}"
    updated, count = pattern.subn(replacement, readme)
    if count != 1:
        raise RuntimeError(f"Markers for {start!r} were not found exactly once.")
    return updated


def main() -> int:
    readme = README.read_text(encoding="utf-8")

    repos = request_json(
        f"https://api.github.com/users/{USERNAME}/repos"
        "?per_page=100&sort=updated&direction=desc"
    )
    if not isinstance(repos, list):
        raise RuntimeError("Unexpected GitHub API response for repositories.")

    readme = replace_section(
        readme, RECENT_START, RECENT_END, render_recent_repos(select_recent(repos))
    )

    # Metrics: never let a metrics hiccup blank the section or fail the run.
    try:
        profile = request_json(f"https://api.github.com/users/{USERNAME}")
        own = owned(repos)
        stars = sum(int(repo.get("stargazers_count", 0)) for repo in own)
        lang_bytes = language_bytes(own)

        contrib: dict[str, Any] | None = None
        if TOKEN:
            try:
                daily = fetch_daily_contributions(profile["created_at"])
                contrib = summarise_contributions(daily)
            except RuntimeError as exc:
                print(f"contributions skipped: {exc}", file=sys.stderr)
        else:
            print("no GITHUB_TOKEN: metrics block left unchanged", file=sys.stderr)

        if TOKEN:
            readme = replace_section(
                readme,
                METRICS_START,
                METRICS_END,
                render_metrics(profile, stars, lang_bytes, contrib),
            )
    except RuntimeError as exc:
        print(f"metrics skipped: {exc}", file=sys.stderr)

    README.write_text(readme, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
