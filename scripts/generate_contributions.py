
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from xml.sax.saxutils import escape

GITHUB_USERNAME = "vanct-official"
GITLAB_USERNAME = "vanct_official"
OUTPUT = "contributions.svg"
DAYS = 365


def request_json(url, headers=None, payload=None):
    data = None
    req_headers = headers or {}

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req_headers = {
            **req_headers,
            "Content-Type": "application/json",
        }

    req = urllib.request.Request(
        url, data=data, headers=req_headers
    )

    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def get_github_counts(start, end):
    token = os.environ["GITHUB_TOKEN"]

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
        payload={"query": query, "variables": variables},
    )

    if result.get("errors"):
        raise RuntimeError(f"GitHub GraphQL error: {result['errors']}")

    user = result.get("data", {}).get("user")
    if not user:
        raise RuntimeError("Không tìm thấy GitHub user.")

    counts = Counter()
    weeks = user["contributionsCollection"]["contributionCalendar"]["weeks"]

    for week in weeks:
        for day in week["contributionDays"]:
            counts[date.fromisoformat(day["date"])] += day["contributionCount"]

    return counts


def get_gitlab_counts(start, end):
    token = os.environ["GITLAB_TOKEN"]
    username = urllib.parse.quote(GITLAB_USERNAME, safe="")
    base_url = f"https://gitlab.com/api/v4/users/{username}/events"

    counts = Counter()
    page = 1

    while page <= 100:
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
            raise RuntimeError("GitLab Events API trả về dữ liệu không hợp lệ.")

        if not events:
            break

        oldest = None

        for event in events:
            timestamp = event.get("created_at")
            if not timestamp:
                continue

            event_date = datetime.fromisoformat(
                timestamp.replace("Z", "+00:00")
            ).date()

            oldest = event_date if oldest is None else min(oldest, event_date)

            if start <= event_date <= end:
                counts[event_date] += 1

        if oldest is not None and oldest < start:
            break

        if len(events) < 100:
            break

        page += 1

    if page > 100:
        print("Warning: GitLab pagination limit reached.")

    return counts


def render_svg(start, end, counts):
    # Align the first column to Sunday.
    first_day = start - timedelta(days=(start.weekday() + 1) % 7)
    last_day = end + timedelta(days=(5 - end.weekday()) % 7)
    weeks = ((last_day - first_day).days + 1) // 7

    values = [
        counts.get(start + timedelta(days=i), 0)
        for i in range((end - start).days + 1)
    ]
    maximum = max(values, default=0)

    def color(value):
        if value == 0:
            return "#ebedf0"
        if maximum == 0:
            return "#9be9a8"
        ratio = value / maximum
        if ratio <= 0.25:
            return "#9be9a8"
        if ratio <= 0.50:
            return "#40c463"
        if ratio <= 0.75:
            return "#30a14e"
        return "#216e39"

    cell = 11
    gap = 3
    left = 35
    top = 30
    width = left + weeks * (cell + gap) + 8
    height = top + 7 * (cell + gap) + 28

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {width} {height}" role="img">',
        "<title>Combined GitHub and GitLab contribution activity</title>",
        '<rect width="100%" height="100%" rx="8" fill="#ffffff"/>',
        '<g font-family="Arial, sans-serif" fill="#24292f">',
        '<text x="8" y="16" font-size="12" font-weight="600">'
        'GitHub + GitLab Contributions</text>',
    ]

    for label, row in [("Mon", 1), ("Wed", 3), ("Fri", 5)]:
        parts.append(
            f'<text x="2" y="{top + row * (cell + gap) + 9}" '
            f'font-size="9" fill="#57606a">{label}</text>'
        )

    for offset in range((last_day - first_day).days + 1):
        current = first_day + timedelta(days=offset)
        col = offset // 7
        row = offset % 7
        value = counts.get(current, 0)

        if start <= current <= end:
            x = left + col * (cell + gap)
            y = top + row * (cell + gap)
            tooltip = escape(f"{current}: {value} contributions")
            parts.append(
                f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" '
                f'rx="2" fill="{color(value)}">'
                f'<title>{tooltip}</title></rect>'
            )

        if current.day == 1:
            x = left + col * (cell + gap)
            parts.append(
                f'<text x="{x}" y="27" font-size="9" '
                f'fill="#57606a">{current.strftime("%b")}</text>'
            )

    parts.append(
        f'<text x="{left}" y="{height - 7}" font-size="9" '
        f'fill="#57606a">Daily combined activity • Last {DAYS} days</text>'
    )
    parts.append("</g></svg>")

    with open(OUTPUT, "w", encoding="utf-8") as file:
        file.write("\n".join(parts))


def main():
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=DAYS - 1)

    github = get_github_counts(start, end)
    gitlab = get_gitlab_counts(start, end)

    combined = Counter(github)
    combined.update(gitlab)

    render_svg(start, end, combined)
    print(f"Generated {OUTPUT} for {start} through {end}")


if __name__ == "__main__":
    main()
