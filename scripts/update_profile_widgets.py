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
    :root { --bg:#0d1117; --border:#30363d; --title:#f0f6fc; --text:#c9d1d9; --muted:#8b949e; --accent:#2f81f7; --track:#21262d; --pill-bg:rgba(46,160,67,.15); --pill-text:#3fb950; --line:#39d353; }
    @media (prefers-color-scheme: light) { :root { --bg:#ffffff; --border:#d0d7de; --title:#1f2328; --text:#1f2328; --muted:#656d76; --accent:#0969da; --track:#d8dee4; --pill-bg:rgba(31,136,61,.12); --pill-text:#1a7f37; --line:#1a7f37; } }
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
    .grid { stroke:var(--track); stroke-width:1; }
    .monthline { stroke:var(--border); stroke-opacity:.35; }
    .area { fill:url(#areaFill); }
    .plot-glow { fill:none; stroke:var(--line); stroke-width:7; stroke-linejoin:round; stroke-linecap:round; opacity:.22; }
    .plot-line { fill:none; stroke:var(--line); stroke-width:1.8; stroke-linejoin:round; stroke-linecap:round; }
    .trend { fill:none; stroke:var(--accent); stroke-width:2.4; stroke-linejoin:round; stroke-linecap:round; }
    .peak-dot { fill:var(--line); }
    .peak-label { fill:var(--title); font-size:11px; font-weight:600; }
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


def streak_and_peak(days: list[dict[str, Any]]) -> tuple[int, int, str]:
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
    return longest, best_count, best_date


def contribution_breakdown(collection: dict[str, Any]) -> str:
    return " · ".join(
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


def weekly_buckets(days: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for day in days:
        weekday = datetime.fromisoformat(day["date"]).weekday()
        if weekday == 0 or current is None:
            current = {"start": day["date"], "days": [], "total": 0}
            buckets.append(current)
        current["days"].append(day)
        current["total"] += day["count"]
    for bucket in buckets:
        bucket["end"] = bucket["days"][-1]["date"]
    return buckets


def bar_path(x: float, y: float, w: float, h: float, r: float) -> str:
    r = min(r, w / 2, max(1.0, h))
    return (
        f"M{x:.1f},{y + h:.1f} L{x:.1f},{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
        f"L{x + w - r:.1f},{y:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} L{x + w:.1f},{y + h:.1f} Z"
    )


def render_weekly_chart(collection: dict[str, Any], private_ok: bool) -> str:
    days = calendar_days(collection)
    if not days:
        days = [{"date": collection["_to"], "count": 0}]

    width, height = 1120, 470
    plot_left, plot_right = 64, width - 28
    plot_top, plot_bottom = 172, 386
    buckets = weekly_buckets(days)
    nb = len(buckets)
    slot = (plot_right - plot_left) / nb
    bar_w = min(16, slot * 0.62)
    totals = [bucket["total"] for bucket in buckets]

    def x(index: int) -> float:
        return plot_left + index * slot + (slot - bar_w) / 2

    def y(count: float) -> float:
        return plot_bottom - (count / nice_max) * (plot_bottom - plot_top)

    maximum = max(totals)
    grid_step = next(s for s in (25, 50, 100, 200, 500, 1000) if maximum / s <= 4)
    nice_max = max(grid_step, -(-maximum // grid_step) * grid_step)

    total = sum(totals)
    longest, best_count, best_date = streak_and_peak(days)
    best_label = f"best day · {format_date(best_date + 'T00:00:00Z')}" if best_count else "best day · –"
    span_days = max(1, (datetime.fromisoformat(days[-1]["date"]) - datetime.fromisoformat(days[0]["date"])).days + 1)
    daily_average = round(total / span_days)

    stats = ""
    for index, (value, label) in enumerate(
        (
            (f"{total:,}", "contributions"),
            (f"{daily_average}", "daily average"),
            (f"{longest} days", "longest streak"),
            (str(best_count), best_label),
        )
    ):
        sx = 24 + index * 272
        stats += f'\n    <text x="{sx}" y="112" class="stat">{html.escape(value)}</text>\n    <text x="{sx}" y="130" class="stat-label">{html.escape(label)}</text>'

    grid = ""
    for tick in range(0, nice_max + 1, grid_step):
        gy = y(tick)
        grid += f'\n    <line x1="{plot_left}" y1="{gy:.1f}" x2="{plot_right}" y2="{gy:.1f}" class="grid" />'
        grid += f'\n    <text x="{plot_left - 10}" y="{gy + 3.5:.1f}" text-anchor="end" class="axis">{tick}</text>'

    monthly: dict[str, int] = {}
    for day in days:
        monthly[day["date"][:7]] = monthly.get(day["date"][:7], 0) + day["count"]

    first_bar_of_month: dict[str, int] = {}
    for index, bucket in enumerate(buckets):
        first_bar_of_month.setdefault(bucket["start"][:7], index)

    months = ""
    for month, index in first_bar_of_month.items():
        mx = x(index) + bar_w / 2
        months += f'\n    <line x1="{mx:.1f}" y1="{plot_top}" x2="{mx:.1f}" y2="{plot_bottom}" class="monthline" />'
        anchor = "middle"
        if mx - 22 < plot_left:
            anchor = "start"
        elif mx + 22 > plot_right:
            anchor = "end"
        label = datetime.fromisoformat(buckets[index]["start"]).strftime("%b")
        months += f'\n    <text x="{mx:.1f}" y="{plot_bottom + 24}" text-anchor="{anchor}" class="axis" style="font-size:11px">{label}</text>'
        months += f'\n    <text x="{mx:.1f}" y="{plot_bottom + 40}" text-anchor="{anchor}" class="axis">{monthly[month]}</text>'

    bars = ""
    for index, bucket in enumerate(buckets):
        bx, bh = x(index), y(bucket["total"])
        if bucket["total"] > 0:
            bars += f'<path d="{bar_path(bx, bh, bar_w, plot_bottom - bh, 3.5)}" fill="url(#barFill)" />'
        else:
            bars += f'<rect x="{bx:.1f}" y="{plot_bottom - 3}" width="{bar_w:.1f}" height="3" rx="1.5" class="track" />'

    average_points = []
    for index in range(nb):
        window = totals[max(0, index - 1) : index + 3]
        cx = x(index) + bar_w / 2
        cy = y(sum(window) / len(window))
        average_points.append(f"{cx:.1f},{cy:.1f}")
    average_path = "M " + " L ".join(average_points)

    peak = ""
    peak_index = totals.index(max(totals)) if totals else 0
    if max(totals) > 0:
        px, py = x(peak_index) + bar_w / 2, y(totals[peak_index])
        anchor = "middle" if plot_left + 70 < px < plot_right - 70 else ("start" if px <= plot_left + 70 else "end")
        tx = px + (-9 if anchor == "end" else (9 if anchor == "start" else 0))
        ty = py - 12 if py > plot_top + 26 else py + 20
        peak = (
            f'\n    <circle cx="{px:.1f}" cy="{py:.1f}" r="3.5" class="peak-dot" />'
            f'\n    <text x="{tx:.1f}" y="{ty:.1f}" text-anchor="{anchor}" class="peak-label">{totals[peak_index]} · wk of {datetime.fromisoformat(buckets[peak_index]["start"]).strftime("%-d %b")}</text>'
        )

    defs = """<defs>
    <linearGradient id="barFill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#39d353" stop-opacity="0.95" style="stop-color:var(--line)" />
      <stop offset="1" stop-color="#39d353" stop-opacity="0.28" style="stop-color:var(--line)" />
    </linearGradient>
  </defs>"""

    legend = f"""
    <rect x="24" y="{height - 24}" width="12" height="12" rx="3" fill="url(#barFill)" />
    <text x="42" y="{height - 14}" class="axis">weekly contributions</text>
    <line x1="170" y1="{height - 18}" x2="190" y2="{height - 18}" class="trend" />
    <text x="196" y="{height - 14}" class="axis">4-week average</text>
    <text x="{width - 24}" y="{height - 14}" text-anchor="end" class="axis">{html.escape(contribution_breakdown(collection))}</text>"""

    body = f"""  {defs}
  {stats}
  {grid}
  {months}
  {bars}
  <path d="{average_path}" class="trend" />{peak}{legend}"""

    subtitle = f"Weekly contributions · last {span_days} days · refreshes daily"
    pill = "includes private contributions" if private_ok else "public contributions only"
    return svg_shell(width, height, "Contributions", subtitle, body, pill)


def bucket_start_of(buckets: list[dict[str, Any]], index: int) -> str:
    return buckets[index]["start"]


def event_summary(event: dict[str, Any]) -> dict[str, Any] | None:
    event_type = event["type"]
    payload = event.get("payload") or {}
    repo = event["repo"]["name"]
    date = event["created_at"]
    url = f"https://github.com/{repo}"

    def title_of(key: str) -> str:
        value = (payload.get(key) or {}).get("title") or ""
        return str(value)[:90]

    def described_action(action: str, what: str, title: str) -> str:
        return f"{action} {what}: {title}" if title else f"{action} {what}"

    if event_type == "PushEvent":
        size = payload.get("distinct_size") or payload.get("size") or 0
        text = f"pushed {size} commit{'s' if size != 1 else ''}" if size else "pushed commits"
        kind = "push"
    elif event_type == "PullRequestEvent":
        text = described_action(payload.get("action", "updated"), "pull request", title_of("pull_request"))
        kind = "pr"
    elif event_type == "IssuesEvent":
        text = described_action(payload.get("action", "updated"), "issue", title_of("issue"))
        kind = "issue"
    elif event_type == "IssueCommentEvent":
        text = described_action("commented on", "issue", title_of("issue"))
        kind = "issue"
    elif event_type == "PullRequestReviewEvent":
        text = described_action("reviewed", "pull request", title_of("pull_request"))
        kind = "review"
    elif event_type == "PullRequestReviewCommentEvent":
        text = described_action("commented on review of", "pull request", title_of("pull_request"))
        kind = "review"
    elif event_type == "ReleaseEvent":
        text = f"released {(payload.get('release') or {}).get('tag_name') or 'a release'}"
        kind = "release"
    elif event_type == "CreateEvent":
        if payload.get("ref_type") == "repository":
            text = "created repository"
        else:
            return None
        kind = "repo"
    else:
        return None

    return {"date": date, "kind": kind, "text": text, "repo": repo, "url": url}


def recent_events(token: str, pages: int = 3) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for page in range(1, pages + 1):
        path = query_path(f"/users/{USERNAME}/events", per_page=100, page=page)
        try:
            batch, _ = api_get(path, token)
        except urllib.error.HTTPError:
            break
        if not batch:
            break
        events.extend(batch)
    summaries = [summary for event in events if (summary := event_summary(event))]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
    return [summary for summary in summaries if summary["date"][:10] >= cutoff]


def group_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse same-day pushes to the same repo into a single feed row."""
    grouped: list[dict[str, Any]] = []
    index_of: dict[tuple[str, str], int] = {}
    for event in events:
        if event["kind"] == "push":
            key = (event["date"][:10], event["repo"])
            if key in index_of:
                grouped[index_of[key]]["count"] += 1
                continue
            event = {**event, "count": 1}
            index_of[key] = len(grouped)
        grouped.append(event)

    for event in grouped:
        if event["kind"] == "push":
            count = event["count"]
            event["text"] = f"{count} push{'es' if count != 1 else ''}"
    grouped.sort(key=lambda item: item["date"], reverse=True)
    return grouped


def build_dashboard(
    collection: dict[str, Any],
    focus: list[dict[str, Any]],
    built: list[dict[str, Any]],
    events: list[dict[str, Any]],
    private_ok: bool,
) -> str:
    days = calendar_days(collection)
    total = sum(day["count"] for day in days)
    longest, best_count, best_date = streak_and_peak(days)
    span_days = max(1, (datetime.fromisoformat(days[-1]["date"]) - datetime.fromisoformat(days[0]["date"])).days + 1)

    monthly: dict[str, int] = {}
    for day in days:
        monthly[day["date"][:7]] = monthly.get(day["date"][:7], 0) + day["count"]

    def with_url(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {**row, "url": f"https://github.com/{USERNAME}/{row['name']}"}
            for row in rows
        ]

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "scope": "public + private" if private_ok else "public only",
        "totals": {
            "total": total,
            "daily_average": round(total / span_days),
            "longest_streak": longest,
            "best_day": {"date": best_date, "count": best_count},
            "commits": collection["totalCommitContributions"],
            "pull_requests": collection["totalPullRequestContributions"],
            "issues": collection["totalIssueContributions"],
            "reviews": collection["totalPullRequestReviewContributions"],
            "repos_created": collection["totalRepositoryContributions"],
        },
        "daily": [{"date": day["date"], "count": day["count"]} for day in days],
        "weekly": [
            {"start": bucket["start"], "end": bucket["end"], "total": bucket["total"]}
            for bucket in weekly_buckets(days)
        ],
        "monthly": [{"month": month, "total": count} for month, count in monthly.items()],
        "focus": with_url(focus),
        "built": with_url(built),
        "events": group_events(events),
    }

    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    template = (ROOT / "scripts" / "dashboard_template.html").read_text(encoding="utf-8")
    return template.replace("__DATA__", data_json)


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
    events = recent_events(token) if private_ok else []

    pill = "includes private" if private_ok else None
    focus_subtitle = f"All repos · last {RECENT_DAYS} days" if private_ok else f"Public repos · last {RECENT_DAYS} days"
    built_subtitle = "Own commits · original repos · default branches" if private_ok else "Own commits · original public repos · default branches"

    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    (ASSET_DIR / "contributions-weekly.svg").write_text(
        render_weekly_chart(collection, private_ok), encoding="utf-8"
    )
    (ASSET_DIR / "current-focus.svg").write_text(
        render_card("Current focus", focus_subtitle, focus, "pushes", pill),
        encoding="utf-8",
    )
    (ASSET_DIR / "most-built.svg").write_text(
        render_card("Most built", built_subtitle, built, "commits", pill),
        encoding="utf-8",
    )
    (ROOT / "index.html").write_text(
        build_dashboard(collection, focus, built, events, private_ok),
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
