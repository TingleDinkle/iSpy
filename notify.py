"""Digest delivery job: send unsent alerts and events to Discord/Slack.

Items are marked as notified only after every configured webhook accepted
them, so failed sends are retried on the next run. With no webhook configured
the digest prints to the terminal and nothing is marked (add a webhook later
and the backlog goes out).

    python notify.py              # send (or print, if no webhooks configured)
    python notify.py --dry-run    # print only, never mark
    python notify.py --mark-only  # mark backlog as seen without sending
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

from sqlalchemy import select, update

from tracker import utf8_console
from tracker.config import settings
from tracker.db import SessionLocal, session_scope
from tracker.models import Alert, App, AppEvent
from tracker.notify import NotifyFailed, format_digest, send_chunks

log = logging.getLogger("notify")


def collect_unsent() -> tuple[list[Alert], list[AppEvent], dict[int, str]]:
    with SessionLocal() as session:
        alerts = list(session.execute(
            select(Alert).where(Alert.notified_at.is_(None))
            .order_by(Alert.alert_date.desc()).limit(500)
        ).scalars())
        events = list(session.execute(
            select(AppEvent).where(AppEvent.notified_at.is_(None))
            .order_by(AppEvent.event_date.desc()).limit(500)
        ).scalars())
        app_ids = {a.app_id for a in alerts} | {e.app_id for e in events if e.app_id}
        labels = {
            a.id: f"{a.name or a.store_app_id} ({a.store})"
            for a in session.execute(select(App).where(App.id.in_(app_ids))).scalars()
        } if app_ids else {}
    return alerts, events, labels


def mark_notified(alerts: list[Alert], events: list[AppEvent]) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    with session_scope() as session:
        if alerts:
            session.execute(update(Alert)
                            .where(Alert.id.in_([a.id for a in alerts]))
                            .values(notified_at=now))
        if events:
            session.execute(update(AppEvent)
                            .where(AppEvent.id.in_([e.id for e in events]))
                            .values(notified_at=now))


def main() -> int:
    utf8_console()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print, don't send or mark")
    parser.add_argument("--mark-only", action="store_true",
                        help="mark everything unsent as seen without sending")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    alerts, events, labels = collect_unsent()
    if not alerts and not events:
        log.info("Nothing new to send.")
        return 0

    chunks = format_digest(alerts, events, labels)

    if args.mark_only:
        mark_notified(alerts, events)
        log.info("Marked %d alert(s) and %d event(s) as seen without sending.",
                 len(alerts), len(events))
        return 0

    if args.dry_run or not (settings.discord_webhook_url or settings.slack_webhook_url):
        for chunk in chunks:
            print(chunk)
            print("-" * 40)
        if not args.dry_run:
            log.info("No webhook configured — printed %d item(s); they stay queued. "
                     "Set DISCORD_WEBHOOK_URL in .env to deliver.",
                     len(alerts) + len(events))
        return 0

    try:
        delivered = send_chunks(chunks)
    except NotifyFailed as exc:
        log.error("Delivery failed — items stay queued for the next run: %s", exc)
        return 1
    mark_notified(alerts, events)
    log.info("Delivered %d alert(s) + %d event(s) in %d message(s) via %s",
             len(alerts), len(events), len(chunks), ", ".join(delivered))
    return 0


if __name__ == "__main__":
    sys.exit(main())
