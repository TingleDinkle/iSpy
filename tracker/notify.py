"""Discord/Slack digest delivery for alerts and intelligence events.

Formatting is pure (unit-testable); delivery is a thin requests wrapper.
Items are marked notified only after every configured sink accepted them, so
a failed send is retried on the next run rather than lost.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Optional, Sequence

import requests

from .config import settings
from .models import Alert, AppEvent

log = logging.getLogger(__name__)

DISCORD_CHUNK_LIMIT = 1900  # hard API limit is 2000 chars per message
MAX_ITEMS_PER_DIGEST = 60

# event_type -> (section, emoji). Ordered by signal density: updates and
# launches are rare and always matter; chart churn is voluminous, so last.
SECTIONS: list[tuple[str, str, tuple[str, ...]]] = [
    ("Updates shipped", "🛠️", ("version_update",)),
    ("Launch radar", "🌍", ("new_developer_app", "soft_launch_detected",
                            "global_launch", "countries_expanded", "countries_reduced")),
    ("UA & creative", "📣", ("ua_start", "ua_stop", "icon_change", "screenshots_change")),
    ("Review signals", "🗣️", ("review_topic_surge",)),
    ("Chart moves", "🚀", ("chart_entry", "rank_jump", "chart_leader_change")),
]


class NotifyFailed(Exception):
    pass


def _alert_line(alert: Alert, app_label: str) -> str:
    direction = "▲" if float(alert.pct_change) >= 0 else "▼"
    return (f"• **{app_label}** — {alert.metric} {direction} "
            f"{abs(float(alert.pct_change)):.0f}% vs {alert.window_days}d baseline "
            f"({float(alert.value):,.0f} vs {float(alert.baseline):,.0f}) "
            f"on {alert.alert_date}")


def format_digest(
    alerts: Sequence[Alert],
    events: Sequence[AppEvent],
    app_labels: dict[int, str],
    today: Optional[dt.date] = None,
) -> list[str]:
    """Build message chunks (each <= DISCORD_CHUNK_LIMIT chars)."""
    today = today or dt.date.today()
    total = len(alerts) + len(events)
    if total == 0:
        return []

    lines: list[str] = [f"**📊 iSpy digest — {today} — {total} item(s)**"]

    shown = 0
    if alerts:
        lines.append("")
        lines.append("**💰 Metric alerts**")
        for alert in alerts:
            if shown >= MAX_ITEMS_PER_DIGEST:
                break
            label = app_labels.get(alert.app_id, f"app #{alert.app_id}")
            lines.append(_alert_line(alert, label))
            shown += 1

    by_type: dict[str, list[AppEvent]] = {}
    for event in events:
        by_type.setdefault(event.event_type, []).append(event)

    known_types = set()
    for section, emoji, types in SECTIONS:
        known_types.update(types)
        section_events = [e for t in types for e in by_type.get(t, [])]
        if not section_events or shown >= MAX_ITEMS_PER_DIGEST:
            continue
        lines.append("")
        lines.append(f"**{emoji} {section}**")
        for event in section_events:
            if shown >= MAX_ITEMS_PER_DIGEST:
                break
            lines.append(f"• {event.title}")
            shown += 1

    leftovers = [e for e in events if e.event_type not in known_types]
    if leftovers and shown < MAX_ITEMS_PER_DIGEST:
        lines.append("")
        lines.append("**📌 Other**")
        for event in leftovers:
            if shown >= MAX_ITEMS_PER_DIGEST:
                break
            lines.append(f"• {event.title}")
            shown += 1

    if total > shown:
        lines.append("")
        lines.append(f"…and {total - shown} more — see the events feed "
                     f"(v_events_feed) in the database/dashboard.")

    # chunk to the Discord limit, never splitting a line
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > DISCORD_CHUNK_LIMIT:
            if current:
                chunks.append(current)
            current = line[:DISCORD_CHUNK_LIMIT]
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _post_with_retry(url: str, payload: dict, sink: str) -> None:
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code < 300:
                return
            last_error = NotifyFailed(f"{sink} returned HTTP {resp.status_code}: {resp.text[:200]}")
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                time.sleep(min(float(retry_after), 30) if retry_after else 2 ** attempt)
                continue
            if 400 <= resp.status_code < 500:
                break  # bad webhook config — retrying won't help
        except requests.RequestException as exc:
            last_error = exc
        time.sleep(2 ** attempt)
    raise NotifyFailed(f"failed to deliver to {sink}: {last_error}")


def send_chunks(chunks: list[str]) -> list[str]:
    """Send to every configured sink. Returns the sink names that succeeded;
    raises NotifyFailed only if a configured sink failed."""
    delivered: list[str] = []
    failures: list[str] = []
    if settings.discord_webhook_url:
        try:
            for chunk in chunks:
                _post_with_retry(settings.discord_webhook_url, {"content": chunk}, "discord")
            delivered.append("discord")
        except NotifyFailed as exc:
            failures.append(str(exc))
    if settings.slack_webhook_url:
        try:
            for chunk in chunks:
                _post_with_retry(settings.slack_webhook_url, {"text": chunk}, "slack")
            delivered.append("slack")
        except NotifyFailed as exc:
            failures.append(str(exc))
    if failures:
        raise NotifyFailed("; ".join(failures))
    return delivered
