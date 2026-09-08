#!/usr/bin/env python3
"""Generate profile widgets from Luis's GitHub activity.

Uses PROFILE_TOKEN (or GITHUB_TOKEN). When the token belongs to Luis,
private contributions and private repository names are included;
otherwise the widgets gracefully fall back to public-only data.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


USERNAME = "luisKisters"
RECENT_DAYS = 30
ROW_LIMIT = 5
YEAR_DAYS = 365
# Add "owner/repo" names here to keep them off the widgets entirely.
EXCLUDED_REPOS: set[str] = set()
ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "assets"
API_ROOT = "https://api.github.com"


def api_get(path: str, token: str, retries: int = 3) -> tuple[Any, dict[str, str]]:
    request = urllib.request.Request(
        f"{API_ROOT}{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": f"{USERNAME}-profile-widget",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response), dict(response.headers.items())
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
            time.sleep(2**attempt)
    raise last_error  # type: ignore[misc]


def graphql(query: str, token: str, **variables: object) -> dict[str, Any]:
    payload = json.dumps({"query": query, "variables": variables}).encode()
    request = urllib.request.Request(
        f"{API_ROOT}/graphql",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": f"{USERNAME}-profile-widget",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.load(response)
    if body.get("errors"):
        raise SystemExit(f"GraphQL error: {body['errors']}")
    return body["data"]


def query_path(path: str, **params: object) -> str:
    return f"{path}?{urllib.parse.urlencode(params)}"


def viewer_login(token: str) -> str:
    data = graphql("query { viewer { login } }", token)
    return data["viewer"]["login"]


def fetch_contributions(token: str) -> dict[str, Any]:
    to_date = datetime.now(timezone.utc)
    from_date = to_date - timedelta(days=YEAR_DAYS)
    query = """
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          totalCommitContributions
          totalPullRequestContributions
          totalIssueContributions
          totalPullRequestReviewContributions
          totalRepositoryContributions
          contributionCalendar {
            weeks {
              contributionDays {
                date
                contributionCount
              }
            }
          }
        }
      }
    }
    """
    variables = {
        "login": USERNAME,
        "from": from_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": to_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    data = graphql(query, token, **variables)
    collection = data["user"]["contributionsCollection"]
    collection["_from"] = from_date.strftime("%Y-%m-%d")
    collection["_to"] = to_date.strftime("%Y-%m-%d")
    return collection


def calendar_days(collection: dict[str, Any]) -> list[dict[str, Any]]:
    start, end = collection["_from"], collection["_to"]
    days = []
    for week in collection["contributionCalendar"]["weeks"]:
        for day in week["contributionDays"]:
            date = day["date"][:10]
            if start <= date <= end:
                days.append({"date": date, "count": day["contributionCount"]})
    return days


def owned_repositories(token: str, private_ok: bool) -> list[dict[str, Any]]:
    if private_ok:
        path = query_path("/user/repos", type="owner", sort="updated", per_page=100)
    else:
        path = query_path(
            f"/users/{USERNAME}/repos", type="owner", sort="updated", per_page=100
        )
    repos, _ = api_get(path, token)
    return [repo for repo in repos if not repo["archived"] and repo["size"] > 0]


def commit_count(repo: str, token: str) -> tuple[int, str | None]:
    path = query_path(f"/repos/{USERNAME}/{repo}/commits", author=USERNAME, per_page=1)
    commits, headers = api_get(path, token)
    if not commits:
        return 0, None

    link = headers.get("Link", "")
    last_page = re.search(r"[?&]page=(\d+)>; rel=\"last\"", link)
    count = int(last_page.group(1)) if last_page else len(commits)
    commit = commits[0].get("commit", {})
    date = (commit.get("author") or {}).get("date") or (commit.get("committer") or {}).get("date")
    return count, date


def recent_pushes(token: str, repo_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)
    events: list[dict[str, Any]] = []

    for page in range(1, 4):
        path = query_path(f"/users/{USERNAME}/events", per_page=100, page=page)
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
        and event["repo"]["name"] in repo_map
        and event["repo"]["name"] != f"{USERNAME}/{USERNAME}"
        and not repo_map[event["repo"]["name"]]["fork"]
        and event["repo"]["name"] not in EXCLUDED_REPOS
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
                "private": repo_map[name]["private"],
            }
            for name, count in counts.items()
        ),
        key=lambda item: (item["value"], last_activity[f"{USERNAME}/{item['name']}"]),
        reverse=True,
    )[:ROW_LIMIT]


def most_built(token: str, repos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    built: list[dict[str, Any]] = []
    for repo in repos:
        if repo["fork"] or repo["name"] == USERNAME or repo["full_name"] in EXCLUDED_REPOS:
            continue
        try:
            count, last_commit = commit_count(repo["name"], token)
        except Exception:
            continue
        if count:
            built.append(
                {
                    "name": repo["name"],
                    "value": count,
                    "detail": format_date(last_commit) if last_commit else "",
                    "private": repo["private"],
                }
            )

    built.sort(key=lambda item: (item["value"], item["name"].lower()), reverse=True)
    return built[:ROW_LIMIT]


def format_date(value: str) -> str:
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return f"{date.day} {date.strftime('%b %Y')}"


SVG_STYLE = """
    :root { --bg:#0d1117; --border:#30363d; --title:#f0f6fc; --text:#c9d1d9; --muted:#8b949e; --accent:#2f81f7; --track:#21262d; --pill-bg:rgba(46,160,67,.15); --pill-text:#3fb950; --cell0:#161b22; --cell1:#0e4429; --cell2:#006d32; --cell3:#26a641; --cell4:#39d353; }
    @media (prefers-color-scheme: light) { :root { --bg:#ffffff; --border:#d0d7de; --title:#1f2328; --text:#1f2328; --muted:#656d76; --accent:#0969da; --track:#d8dee4; --pill-bg:rgba(31,136,61,.12); --pill-text:#1a7f37; --cell0:#ebedf0; --cell1:#9be9a8; --cell2:#40c463; --cell3:#30a14e; --cell4:#216e39; } }
    text { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif; }
    .title { fill:var(--title); font-size:20px; font-weight:700; }
    .subtitle,.detail,.rank,.axis { fill:var(--muted); }
    .subtitle,.detail,.rank { font-size:12px; }
    .axis { font-size:10px; }
    .repo { fill:var(--text); font-size:14px; font-weight:600; }
    .value { fill:var(--text); font-size:13px; }
    .track { fill:var(--track); }
    .bar { fill:var(--accent); }
    .stat { fill:var(--title); font-size:20px; font-weight:700; }
    .stat-label { fill:var(--muted); font-size:11px; }
    .pill { fill:var(--pill-bg); }
    .pill-text { fill:var(--pill-text); font-size:11px; font-weight:600; }
    .cell0 { fill:var(--cell0); }
    .cell1 { fill:var(--cell1); }
    .cell2 { fill:var(--cell2); }
    .cell3 { fill:var(--cell3); }
    .cell4 { fill:var(--cell4); }
"""


def svg_shell(width: int, height: int, title: str, subtitle: str, body: str, pill: str | None = None) -> str:
    pill_markup = ""
    if pill:
        pill_width = len(pill) * 6.1 + 22
        pill_x = width - 24 - pill_width
        pill_markup = f"""
  <rect x="{pill_x:.0f}" y="18" width="{pill_width:.0f}" height="20" rx="10" class="pill" />
  <text x="{pill_x + pill_width / 2:.0f}" y="31.5" text-anchor="middle" class="pill-text">{html.escape(pill)}</text>"""
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">
  <title id="title">{html.escape(title)}</title>
  <desc id="desc">{html.escape(subtitle)}</desc>
  <style>{SVG_STYLE}</style>
  <rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="12" fill="var(--bg)" stroke="var(--border)" />
  <text x="24" y="34" class="title">{html.escape(title)}</text>
  <text x="24" y="56" class="subtitle">{html.escape(subtitle)}</text>{pill_markup}
{body}
</svg>
"""


def render_card(title: str, subtitle: str, rows: list[dict[str, Any]], value_label: str, pill: str | None) -> str:
    width = 560
    height = 92 + 48 * len(rows)
    maximum = max((int(row["value"]) for row in rows), default=1)
    row_markup: list[str] = []

    for index, row in enumerate(rows, start=1):
        y = 92 + (index - 1) * 48
        bar_width = max(10, round(180 * int(row["value"]) / maximum))
        name = html.escape(str(row["name"]))
        detail = html.escape(str(row.get("detail", "")))
        if row.get("private"):
            detail += " · private"
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

    empty_state = "" if rows else '<text x="24" y="98" class="repo">No activity in this window.</text>'
    body = "".join(row_markup) + f"\n  {empty_state}"
    return svg_shell(width, height, title, subtitle, body, pill)


def render_calendar(collection: dict[str, Any], private_ok: bool) -> str:
    days = calendar_days(collection)
    total = sum(day["count"] for day in days)
    if not days:
        days = [{"date": collection["_to"], "count": 0}]

    cell, gap = 15, 3
    pitch = cell + gap
    grid_left, grid_top = 44, 178
    weeks = collection["contributionCalendar"]["weeks"]

    columns = len(weeks)
    grid_width = columns * pitch
    width = grid_left + grid_width + 24
    grid_bottom = grid_top + 7 * pitch
    legend_y = grid_bottom + 30
    height = legend_y + 22

    counts = [day["count"] for day in days]
    nonzero = sorted(count for count in counts if count > 0)
    if nonzero:
        q1, q2, q3 = nonzero[len(nonzero) // 4], nonzero[len(nonzero) // 2], nonzero[3 * len(nonzero) // 4]
    else:
        q1 = q2 = q3 = 1

    def level(count: int) -> int:
        if count <= 0:
            return 0
        if count > q3:
            return 4
        if count > q2:
            return 3
        if count > q1:
            return 2
        return 1

    def cell_class(count: int) -> str:
        return f"cell{level(count)}"

    # Longest streak and best day
    longest = streak = 0
    best_count, best_date = 0, ""
    for day in days:
        if day["count"] > 0:
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 0
        if day["count"] > best_count:
            best_count, best_date = day["count"], day["date"]

    best_label = f"best day · {format_date(best_date + 'T00:00:00Z')}" if best_count else "best day · –"
    span_days = max(1, (datetime.fromisoformat(days[-1]["date"]) - datetime.fromisoformat(days[0]["date"])).days + 1)
    daily_average = round(total / span_days)

    cells: list[str] = []
    month_labels: list[tuple[int, str]] = []
    last_month: int | None = None
    for col, week in enumerate(weeks):
        week_days = [day for day in week["contributionDays"] if collection["_from"] <= day["date"][:10] <= collection["_to"]]
        for day in week["contributionDays"]:
            date = day["date"][:10]
            if not (collection["_from"] <= date <= collection["_to"]):
                continue
            weekday = datetime.fromisoformat(date).weekday() + 1  # 1=Mon … 7=Sun
            row = weekday % 7  # 0=Sun … 6=Sat
            x = grid_left + col * pitch
            y = grid_top + row * pitch
            cells.append(
                f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="3.5" class="{cell_class(day["contributionCount"])}"><title>{day["contributionCount"]} contribution{"s" if day["contributionCount"] != 1 else ""} on {date}</title></rect>'
            )
        first = week_days[0]["date"][:10] if week_days else None
        for day in week_days:
            date = day["date"][:10]
            if not date.endswith("-01"):
                continue
            month = int(date[5:7])
            if month != last_month:
                month_labels.append((col, datetime.fromisoformat(date).strftime("%b")))
                last_month = month
            break

    month_markup = "".join(
        f'\n    <text x="{grid_left + col * pitch}" y="{grid_top - 8}" class="axis">{name}</text>'
        for col, name in month_labels
    )
    weekday_labels = ""
    for row, label in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        weekday_labels += f'\n    <text x="{grid_left - 10}" y="{grid_top + row * pitch + 11}" text-anchor="end" class="axis">{label}</text>'

    breakdown = " · ".join(
        part
        for part in (
            f"{collection['totalCommitContributions']} commits" if collection["totalCommitContributions"] else "",
            f"{collection['totalPullRequestContributions']} pull requests" if collection["totalPullRequestContributions"] else "",
            f"{collection['totalIssueContributions']} issues" if collection["totalIssueContributions"] else "",
            f"{collection['totalPullRequestReviewContributions']} reviews" if collection["totalPullRequestReviewContributions"] else "",
            f"{collection['totalRepositoryContributions']} repos created" if collection["totalRepositoryContributions"] else "",
        )
        if part
    )

    legend_cells = "".join(
        f'<rect x="{368 + index * 20}" y="{legend_y - 12}" width="{cell}" height="{cell}" rx="3.5" class="cell{index}" />'
        for index in range(5)
    )
    legend = f"""
    <text x="24" y="{legend_y}" class="axis">Less</text>
    {legend_cells}
    <text x="{368 + 5 * 20 + 4}" y="{legend_y}" class="axis">More</text>
    <text x="{width - 24}" y="{legend_y}" text-anchor="end" class="axis">{html.escape(breakdown)}</text>"""

    stats = ""
    for index, (value, label) in enumerate(
        (
            (f"{total:,}", "contributions"),
            (f"{daily_average}", "daily average"),
            (f"{longest} days", "longest streak"),
            (str(best_count), best_label),
        )
    ):
        x = 24 + index * 248
        stats += f'\n    <text x="{x}" y="112" class="stat">{html.escape(value)}</text>\n    <text x="{x}" y="130" class="stat-label">{html.escape(label)}</text>'

    body = f"""  {stats}
  {month_markup}
  {weekday_labels}
  {''.join(cells)}{legend}"""

    subtitle = f"Last {span_days} days · refreshes daily"
    pill = "includes private contributions" if private_ok else "public contributions only"
    return svg_shell(width, height, "Contributions", subtitle, body, pill)


def main() -> None:
    token = os.environ.get("PROFILE_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("PROFILE_TOKEN or GITHUB_TOKEN is required")

    try:
        private_ok = viewer_login(token).lower() == USERNAME.lower()
    except Exception:
        private_ok = False
    scope = "public + private" if private_ok else "public only"
    print(f"Data scope: {scope}")

    repos = owned_repositories(token, private_ok)
    repo_map = {repo["full_name"]: repo for repo in repos}
    focus = recent_pushes(token, repo_map)
    built = most_built(token, repos)
    collection = fetch_contributions(token)

    pill = "includes private" if private_ok else None
    focus_subtitle = f"All repos · last {RECENT_DAYS} days" if private_ok else f"Public repos · last {RECENT_DAYS} days"
    built_subtitle = "Own commits · original repos · default branches" if private_ok else "Own commits · original public repos · default branches"

    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    (ASSET_DIR / "contributions-calendar.svg").write_text(
        render_calendar(collection, private_ok), encoding="utf-8"
    )
    (ASSET_DIR / "current-focus.svg").write_text(
        render_card("Current focus", focus_subtitle, focus, "pushes", pill),
        encoding="utf-8",
    )
    (ASSET_DIR / "most-built.svg").write_text(
        render_card("Most built", built_subtitle, built, "commits", pill),
        encoding="utf-8",
    )

    snapshot = {
        "scope": scope,
        "current_focus": focus,
        "most_built": built,
        "contributions": {
            "total": sum(day["count"] for day in calendar_days(collection)),
            "commits": collection["totalCommitContributions"],
            "pull_requests": collection["totalPullRequestContributions"],
            "issues": collection["totalIssueContributions"],
        },
    }
    (ASSET_DIR / "project-activity.json").write_text(
        json.dumps(snapshot, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
