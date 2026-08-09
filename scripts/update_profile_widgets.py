#!/usr/bin/env python3
"""Generate profile widgets from Luis's public GitHub activity."""

from __future__ import annotations

import html
import json
import os
import re
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


USERNAME = "luisKisters"
RECENT_DAYS = 30
ROW_LIMIT = 5
ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "assets"
API_ROOT = "https://api.github.com"


def api_get(path: str, token: str) -> tuple[Any, dict[str, str]]:
    request = urllib.request.Request(
        f"{API_ROOT}{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": f"{USERNAME}-profile-widget",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response), dict(response.headers.items())


def query_path(path: str, **params: object) -> str:
    return f"{path}?{urllib.parse.urlencode(params)}"


def commit_count(repo: str, token: str) -> tuple[int, str | None]:
    path = query_path(
        f"/repos/{USERNAME}/{repo}/commits",
        author=USERNAME,
        per_page=1,
    )
    commits, headers = api_get(path, token)
    if not commits:
        return 0, None

    link = headers.get("Link", "")
    last_page = re.search(r"[?&]page=(\d+)>; rel=\"last\"", link)
    count = int(last_page.group(1)) if last_page else len(commits)
    commit = commits[0].get("commit", {})
    date = (commit.get("author") or {}).get("date") or (commit.get("committer") or {}).get("date")
    return count, date


def public_repositories(token: str) -> list[dict[str, Any]]:
    path = query_path(
        f"/users/{USERNAME}/repos",
        type="owner",
        sort="updated",
        direction="desc",
        per_page=100,
    )
    repos, _ = api_get(path, token)
    return [repo for repo in repos if not repo["archived"] and repo["size"] > 0]


def recent_pushes(token: str, public_names: set[str]) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)
    events: list[dict[str, Any]] = []

    for page in range(1, 4):
        path = query_path(f"/users/{USERNAME}/events/public", per_page=100, page=page)
        batch, _ = api_get(path, token)
        if not batch:
            break
        events.extend(batch)
        oldest = datetime.fromisoformat(batch[-1]["created_at"].replace("Z", "+00:00"))
        if oldest < cutoff:
            break

    pushes = [
        event
        for event in events
        if event["type"] == "PushEvent"
        and event["repo"]["name"] in public_names
        and event["repo"]["name"] != f"{USERNAME}/{USERNAME}"
        and datetime.fromisoformat(event["created_at"].replace("Z", "+00:00")) >= cutoff
    ]

    counts = Counter(event["repo"]["name"] for event in pushes)
    last_activity: dict[str, str] = {}
    for event in pushes:
        name = event["repo"]["name"]
        last_activity[name] = max(last_activity.get(name, ""), event["created_at"])

    return sorted(
        (
            {
                "name": name.removeprefix(f"{USERNAME}/"),
                "value": count,
                "detail": format_date(last_activity[name]),
            }
            for name, count in counts.items()
        ),
        key=lambda item: (item["value"], last_activity[f"{USERNAME}/{item['name']}"]),
        reverse=True,
    )[:ROW_LIMIT]


def format_date(value: str) -> str:
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return f"{date.day} {date.strftime('%b %Y')}"


def render_card(title: str, subtitle: str, rows: list[dict[str, Any]], value_label: str) -> str:
    width = 560
    height = 92 + 48 * len(rows)
    maximum = max((int(row["value"]) for row in rows), default=1)
    row_markup: list[str] = []

    for index, row in enumerate(rows, start=1):
        y = 92 + (index - 1) * 48
        bar_width = max(10, round(180 * int(row["value"]) / maximum))
        name = html.escape(str(row["name"]))
        detail = html.escape(str(row.get("detail", "")))
        value = html.escape(f"{row['value']} {value_label}")
        row_markup.append(
            f"""
    <text x="24" y="{y}" class="rank">{index:02d}</text>
    <text x="58" y="{y}" class="repo">{name}</text>
    <text x="536" y="{y}" text-anchor="end" class="value">{value}</text>
    <rect x="58" y="{y + 10}" width="180" height="4" rx="2" class="track" />
    <rect x="58" y="{y + 10}" width="{bar_width}" height="4" rx="2" class="bar" />
    <text x="536" y="{y + 15}" text-anchor="end" class="detail">{detail}</text>"""
        )

    empty_state = "" if rows else '<text x="24" y="98" class="repo">No public activity yet.</text>'
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">
  <title id="title">{html.escape(title)}</title>
  <desc id="desc">{html.escape(subtitle)}</desc>
  <style>
    :root {{ --bg:#0d1117; --border:#30363d; --title:#f0f6fc; --text:#c9d1d9; --muted:#8b949e; --accent:#2f81f7; --track:#21262d; }}
    @media (prefers-color-scheme: light) {{ :root {{ --bg:#ffffff; --border:#d0d7de; --title:#1f2328; --text:#1f2328; --muted:#656d76; --accent:#0969da; --track:#d8dee4; }} }}
    text {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif; }}
    .title {{ fill:var(--title); font-size:20px; font-weight:700; }}
    .subtitle,.detail,.rank {{ fill:var(--muted); font-size:12px; }}
    .repo {{ fill:var(--text); font-size:14px; font-weight:600; }}
    .value {{ fill:var(--text); font-size:13px; }}
    .track {{ fill:var(--track); }}
    .bar {{ fill:var(--accent); }}
  </style>
  <rect x="0.5" y="0.5" width="559" height="{height - 1}" rx="12" fill="var(--bg)" stroke="var(--border)" />
  <text x="24" y="34" class="title">{html.escape(title)}</text>
  <text x="24" y="56" class="subtitle">{html.escape(subtitle)}</text>
  {''.join(row_markup)}
  {empty_state}
</svg>
"""


def main() -> None:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN is required")

    repos = public_repositories(token)
    repo_map = {repo["full_name"]: repo for repo in repos}
    focus = recent_pushes(token, set(repo_map))

    all_time: list[dict[str, Any]] = []
    for repo in repos:
        if repo["fork"] or repo["name"] == USERNAME:
            continue
        count, last_commit = commit_count(repo["name"], token)
        if count:
            all_time.append(
                {
                    "name": repo["name"],
                    "value": count,
                    "detail": format_date(last_commit) if last_commit else "",
                }
            )

    all_time.sort(key=lambda item: (item["value"], item["name"].lower()), reverse=True)
    all_time = all_time[:ROW_LIMIT]

    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    (ASSET_DIR / "current-focus.svg").write_text(
        render_card(
            "Current focus",
            f"Public push activity · last {RECENT_DAYS} days · refreshes daily",
            focus,
            "pushes",
        ),
        encoding="utf-8",
    )
    (ASSET_DIR / "most-built.svg").write_text(
        render_card(
            "Most built",
            "Own commits · original public repos · default branches",
            all_time,
            "commits",
        ),
        encoding="utf-8",
    )

    snapshot = {"current_focus": focus, "most_built": all_time}
    (ASSET_DIR / "project-activity.json").write_text(
        json.dumps(snapshot, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
