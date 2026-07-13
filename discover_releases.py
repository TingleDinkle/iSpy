"""Genre-wide breakout sweep: find NEW releases gaining real traction in your
watched categories — from ANY studio, not just the ones you track.

For every watched (store, category) chart, queries apps released in the last
BREAKOUT_RELEASE_DAYS with >= BREAKOUT_MIN_DOWNLOADS monthly downloads and
emits a 'breakout_release' event per newcomer (deduped, so each game is
reported once ever). ~1 credit per watched category.

Run weekly (wired into run_weekly.bat):
    python discover_releases.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

from sqlalchemy import select

from tracker import utf8_console
from tracker.api_client import AppstoreSpyClient, AppstoreSpyError
from tracker.config import settings
from tracker.db import SessionLocal, session_scope
from tracker.events import store_url
from tracker.ingest import insert_event
from tracker.models import App, RankingWatch

log = logging.getLogger("discover_releases")


def sweep_category(client: AppstoreSpyClient, store: str, category: str,
                   today: dt.date) -> int:
    cutoff = today - dt.timedelta(days=settings.breakout_release_days)
    rows = client.query_apps(
        store,
        {
            "category": category,
            "release_date": {"from": cutoff.isoformat()},
            "downloads_month": {"from": settings.breakout_min_downloads},
        },
        fields=["id", "name", "developer_name", "release_date", "downloads_month"],
        sort="-downloads_month",
        limit=25,
    )
    created = 0
    with session_scope() as session:
        tracked = {
            a for (a,) in session.execute(
                select(App.store_app_id).where(App.store == store))
        }
        for row in rows:
            app_id = str(row.get("id"))
            if not app_id or app_id in tracked:
                continue  # already on the radar via tracking or studio watch
            name = row.get("name") or app_id
            downloads = row.get("downloads_month") or 0
            dev = row.get("developer_name") or "unknown studio"
            if insert_event(
                session,
                event_type="breakout_release",
                event_date=today,
                title=(f"BREAKOUT in {category}: {name} by {dev} — "
                       f"{downloads:,}/mo downloads, released "
                       f"{row.get('release_date')} {store_url(store, app_id)}"),
                details={"app": app_id, "developer": dev,
                         "downloads_month": downloads,
                         "release_date": row.get("release_date"),
                         "category": category},
                dedupe_key=f"breakout_release|{store}|{app_id}",
                store=store, store_app_id=app_id,
            ):
                created += 1
                log.info("breakout: %s (%s) %s dl/mo", name, store, f"{downloads:,}")
    return created


def main() -> int:
    utf8_console()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    today = dt.datetime.now(dt.timezone.utc).date()
    with SessionLocal() as session:
        watches = list(session.execute(
            select(RankingWatch).where(RankingWatch.is_active.is_(True))
        ).scalars())
    if not watches:
        log.error("No chart watches — add one: python manage.py add-watch ios US GAMES_PUZZLE")
        return 1

    client = AppstoreSpyClient()
    total = 0
    failures = 0
    for watch in watches:
        try:
            total += sweep_category(client, watch.store, watch.category, today)
        except AppstoreSpyError as exc:
            failures += 1
            log.error("sweep %s/%s failed: %s", watch.store, watch.category, exc)

    log.info("Done: %d breakout release(s) found. Send with: python notify.py", total)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
