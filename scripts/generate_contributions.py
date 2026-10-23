
import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape


# ============================================================
# Configuration
# ============================================================

GITHUB_USERNAME = "vanct-official"
GITLAB_USERNAME = "vanct_official"

OUTPUT_FILE = Path("contributions.svg")
LOGO_FILE = Path("assets/logo.png")

DAYS = 365
CELL_SIZE = 11
CELL_GAP = 3

# GitHub-inspired dark palette
BACKGROUND = "#0d1117"
PANEL = "#161b22"
BORDER = "#30363d"
TEXT = "#e6edf3"
MUTED = "#8b949e"

LEVEL_COLORS = [
    "#21262d",  # No activity
    "#0e4429",  # Low
    "#006d32",  # Moderate
    "#26a641",  # High
    "#39d353",  # Very high
]


# ============================================================
# HTTP helpers
# ============================================================

def request_json(url, headers=None, payload=None):
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "Combined-Contribution-Dashboard",
        **(headers or {}),
    }

    data = None

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        url,
        data=data,
        headers=request_headers,
        method="POST" if payload is not None else "GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    except urllib.error.HTTPError as exc:
        # Never print request headers or secret values.
        raise RuntimeError(
            f"API request failed with HTTP {exc.code}: "
            f"{urllib.parse.urlsplit(url).netloc}"
        ) from None

    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"API connection failed: {exc.reason}"
        ) from None


# ============================================================
# GitHub GraphQL API
# ============================================================

def get_github_counts(start, end):
    token = os.environ.get("GITHUB_TOKEN")

    if not token:
        raise RuntimeError("Missing GITHUB_TOKEN environment secret.")

    query = """
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
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
        "login": GITHUB_USERNAME,
        "from": f"{start.isoformat()}T00:00:00Z",
        "to": f"{end.isoformat()}T23:59:59Z",
    }

    result = request_json(
        "https://api.github.com/graphql",
        headers={"Authorization": f"Bearer {token}"},
        payload={
            "query": query,
            "variables": variables,
        },
    )

    if result.get("errors"):
        messages = [
            error.get("message", "Unknown GraphQL error")
            for error in result["errors"]
        ]
        raise RuntimeError(
            "GitHub GraphQL error: " + "; ".join(messages)
        )

    user = result.get("data", {}).get("user")

    if not user:
        raise RuntimeError(
            f"GitHub user '{GITHUB_USERNAME}' was not found."
        )

    calendar = user["contributionsCollection"]["contributionCalendar"]
    counts = Counter()

    for week in calendar["weeks"]:
        for item in week["contributionDays"]:
            day = date.fromisoformat(item["date"])
            counts[day] += int(item["contributionCount"])

    return counts


# ============================================================
# GitLab Events API
# ============================================================

def get_gitlab_counts(start, end):
    token = os.environ.get("GITLAB_TOKEN")

    if not token:
        raise RuntimeError("Missing GITLAB_TOKEN environment secret.")

    username = urllib.parse.quote(GITLAB_USERNAME, safe="")
    base_url = (
        f"https://gitlab.com/api/v4/users/{username}/events"
    )

    counts = Counter()
    page = 1
    max_pages = 100

    while page <= max_pages:
        params = urllib.parse.urlencode({
            "after": start.isoformat(),
            "before": end.isoformat(),
            "per_page": 100,
            "page": page,
        })

        events = request_json(
            f"{base_url}?{params}",
            headers={"PRIVATE-TOKEN": token},
        )

        if not isinstance(events, list):
            raise RuntimeError(
                "GitLab Events API returned unexpected data."
            )

        if not events:
            break

        oldest_event_date = None

        for event in events:
            timestamp = event.get("created_at")

            if not timestamp:
                continue

            event_datetime = datetime.fromisoformat(
                timestamp.replace("Z", "+00:00")
            )

            # GitLab timestamps are UTC; group by UTC date.
            event_date = event_datetime.astimezone(
                timezone.utc
            ).date()

            if (
                oldest_event_date is None
                or event_date < oldest_event_date
            ):
                oldest_event_date = event_date

            if start <= event_date <= end:
                counts[event_date] += 1

        # Events are normally returned newest first.
        if oldest_event_date and oldest_event_date < start:
            break

        if len(events) < 100:
            break

        page += 1

    if page > max_pages:
        print(
            "Warning: GitLab pagination limit reached; "
            "some events may not be included."
        )

    return counts


# ============================================================
# Statistics
# ============================================================

def calculate_statistics(start, end, counts):
    all_days = [
        start + timedelta(days=offset)
        for offset in range((end - start).days + 1)
    ]

    values = [max(0, counts.get(day, 0)) for day in all_days]

    total = sum(values)
    active_days = sum(value > 0 for value in values)
    peak = max(values, default=0)

    # Current streak includes today, or yesterday if today
    # has no activity yet.
    streak_end = end

    if counts.get(streak_end, 0) <= 0:
        streak_end -= timedelta(days=1)

    current_streak = 0
    cursor = streak_end

    while cursor >= start and counts.get(cursor, 0) > 0:
        current_streak += 1
        cursor -= timedelta(days=1)

    longest_streak = 0
    running_streak = 0

    for day in all_days:
        if counts.get(day, 0) > 0:
            running_streak += 1
            longest_streak = max(longest_streak, running_streak)
        else:
            running_streak = 0

    return {
        "total": total,
        "active_days": active_days,
        "current_streak": current_streak,
        "longest_streak": longest_streak,
        "peak": peak,
    }


def get_level(value, maximum):
    if value <= 0:
        return 0

    if maximum <= 0:
        return 1

    ratio = value / maximum

    if ratio <= 0.25:
        return 1
    if ratio <= 0.50:
        return 2
    if ratio <= 0.75:
        return 3

    return 4


# ============================================================
# SVG helpers
# ============================================================

def xml_text(value):
    return escape(str(value))


def svg_text(x, y, value, size=12, color=TEXT, weight=400,
             anchor="start"):
    return (
        f'<text x="{x}" y="{y}" '
        f'font-family="Arial, Helvetica, sans-serif" '
        f'font-size="{size}" font-weight="{weight}" '
        f'fill="{color}" text-anchor="{anchor}">'
        f'{xml_text(value)}</text>'
    )


def svg_rect(x, y, width, height, fill, radius=0,
             stroke=None):
    stroke_attr = (
        f' stroke="{stroke}" stroke-width="1"'
        if stroke else ""
    )

    return (
        f'<rect x="{x}" y="{y}" width="{width}" '
        f'height="{height}" rx="{radius}" '
        f'fill="{fill}"{stroke_attr}/>'
    )


def load_logo_data_uri():
    """
    Optional PNG logo embedded as a data URI.

    GitHub Actions does not require this file to exist.
    When absent, the dashboard uses a V monogram.
    """
    if not LOGO_FILE.is_file():
        return None

    encoded = base64.b64encode(LOGO_FILE.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def render_svg(start, end, counts, github_total, gitlab_total):
    stats = calculate_statistics(start, end, counts)

    # Align calendar to Sunday.
    first_day = start - timedelta(
        days=(start.weekday() + 1) % 7
    )
    last_day = end + timedelta(
        days=(5 - end.weekday()) % 7
    )

    total_calendar_days = (last_day - first_day).days + 1
    weeks = total_calendar_days // 7

    left = 36
    top = 192
    cell_step = CELL_SIZE + CELL_GAP

    grid_width = weeks * cell_step
    width = max(760, left + grid_width + 28)
    height = top + 7 * cell_step + 76

    max_value = max(counts.values(), default=0)
    parts = []

    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img">'
    )

    parts.append(
        '<title>Combined GitHub and GitLab Contribution Dashboard</title>'
    )
    parts.append(
        '<desc>Daily activity counts aggregated from GitHub '
        'contributions and GitLab events over the last 365 days.</desc>'
    )

    parts.append(svg_rect(0, 0, width, height, BACKGROUND, 12))

    # Header
    parts.append(svg_text(
        24, 34, "CONTRIBUTION DASHBOARD", 18, TEXT, 700
    ))
    parts.append(svg_text(
        24, 56,
        f"{GITHUB_USERNAME}  +  GitLab / {GITLAB_USERNAME}",
        11, MUTED
    ))
    parts.append(svg_text(
        24, 78,
        f"{start.strftime('%d %b %Y')} — {end.strftime('%d %b %Y')}",
        11, MUTED
    ))

    # Optional personal logo at top-right.
    logo_data = load_logo_data_uri()
    logo_size = 44
    logo_x = width - 24 - logo_size
    logo_y = 20

    if logo_data:
        parts.append(
            f'<image x="{logo_x}" y="{logo_y}" '
            f'width="{logo_size}" height="{logo_size}" '
            f'href="{logo_data}" preserveAspectRatio="xMidYMid meet"/>'
        )
    else:
        parts.append(
            f'<circle cx="{logo_x + logo_size / 2}" '
            f'cy="{logo_y + logo_size / 2}" '
            f'r="{logo_size / 2}" fill="#238636"/>'
        )
        parts.append(svg_text(
            logo_x + logo_size / 2,
            logo_y + 29,
            "V", 24, "#ffffff", 700, "middle"
        ))

    # Statistics cards
    card_gap = 12
    card_y = 96
    card_h = 76
    card_count = 4
    card_width = (width - 48 - card_gap * (card_count - 1)) / card_count

    cards = [
        ("TOTAL ACTIVITY", stats["total"], "#39d353"),
        ("ACTIVE DAYS", stats["active_days"], "#26a641"),
        ("CURRENT STREAK", stats["current_streak"], "#58a6ff"),
        ("BEST DAY", stats["peak"], "#d2a8ff"),
    ]

    for index, (label, value, accent) in enumerate(cards):
        x = 24 + index * (card_width + card_gap)

        parts.append(svg_rect(
            x, card_y, card_width, card_h, PANEL, 8, BORDER
        ))
        parts.append(svg_text(
            x + 12, card_y + 22, label, 9, MUTED, 600
        ))
        parts.append(svg_text(
            x + 12, card_y + 53, f"{value:,}", 22, accent, 700
        ))

    # Calendar heading
    parts.append(svg_text(
        24, 187, "DAILY ACTIVITY", 11, TEXT, 700
    ))

    # Weekday labels
    for label, row in [("Mon", 1), ("Wed", 3), ("Fri", 5)]:
        y = top + row * cell_step + 9
        parts.append(svg_text(5, y, label, 9, MUTED))

    # Month labels
    for offset in range(total_calendar_days):
        current = first_day + timedelta(days=offset)
        col = offset // 7

        if current.day == 1 and start <= current <= end:
            x = left + col * cell_step
            parts.append(svg_text(
                x, top - 8, current.strftime("%b"), 9, MUTED
            ))

    # Contribution squares with accessible tooltips.
    for offset in range(total_calendar_days):
        current = first_day + timedelta(days=offset)

        if current < start or current > end:
            continue

        col = offset // 7
        row = offset % 7
        value = counts.get(current, 0)
        level = get_level(value, max_value)

        x = left + col * cell_step
        y = top + row * cell_step

        parts.append(
            f'<rect x="{x}" y="{y}" width="{CELL_SIZE}" '
            f'height="{CELL_SIZE}" rx="2" '
            f'fill="{LEVEL_COLORS[level]}">'
            f'<title>{xml_text(current.isoformat())}: '
            f'{value} combined activities</title></rect>'
        )

    # Legend
    legend_y = top + 7 * cell_step + 26

    parts.append(svg_text(
        24, legend_y, "Less", 9, MUTED
    ))

    legend_start_x = 59

    for level, color in enumerate(LEVEL_COLORS):
        x = legend_start_x + level * (CELL_SIZE + 4)

        parts.append(svg_rect(
            x, legend_y - 10, CELL_SIZE, CELL_SIZE, color, 2
        ))

    parts.append(svg_text(
        legend_start_x + 5 * (CELL_SIZE + 4) + 4,
        legend_y,
        "More",
        9,
        MUTED
    ))

    # Footer
    footer_y = height - 15
    parts.append(svg_text(
        24, footer_y,
        f"GitHub: {github_total:,} contributions  |  "
        f"GitLab: {gitlab_total:,} events  |  "
        "Updated automatically with GitHub Actions",
        9, MUTED
    ))

    parts.append("</svg>")

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text("\n".join(parts), encoding="utf-8")


# ============================================================
# Main
# ============================================================

def main():
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=DAYS - 1)

    print(f"Fetching GitHub activity for {GITHUB_USERNAME}...")
    github_counts = get_github_counts(start, end)

    print(f"Fetching GitLab events for {GITLAB_USERNAME}...")
    gitlab_counts = get_gitlab_counts(start, end)

    combined_counts = Counter(github_counts)
    combined_counts.update(gitlab_counts)

    github_total = sum(github_counts.values())
    gitlab_total = sum(gitlab_counts.values())

    render_svg(
        start,
        end,
        combined_counts,
        github_total,
        gitlab_total,
    )

    print(f"Generated {OUTPUT_FILE}")
    print(f"GitHub contributions: {github_total}")
    print(f"GitLab events: {gitlab_total}")


if __name__ == "__main__":
    main()
