"""Event detection job (DB-only, zero API credits): compares the last two
daily snapshots of every tracked app and the last two days of every watched
chart, and writes intelligence events to ``app_events``.

Detects: version updates (with patch notes), icon/screenshot changes, UA
(advertised) flips, storefront expansion/reduction, soft-launch -> global
launch, chart entries, rank jumps, and #1 changes.

Run daily, after daily_snapshot.py:
    python detect_events.py
"""

from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import select

from tracker import utf8_console
from tracker.db import SessionLocal, session_scope
from tracker.events import (
    analyze_rank_moves,
    diff_snapshots,
    entry_top_for_collection,
    store_url,
)
from tracker.ingest import insert_event
from tracker.models import App, AppSnapshot, Ranking, RankingWatch, STORE_IOS

log = logging.getLogger("detect_events")


def detect_snapshot_events() -> int:
    """Diff the two most recent snapshots of every active app."""
    created = 0
    with SessionLocal() as session:
        apps = list(session.execute(
            select(App).where(App.is_active.is_(True))
        ).scalars())

    for app in apps:
        with session_scope() as session:
            last_two = list(session.execute(
                select(AppSnapshot)
                .where(AppSnapshot.app_id == app.id)
                .order_by(AppSnapshot.snapshot_date.desc())
                .limit(2)
            ).scalars())
            if len(last_two) < 2:
                continue
            curr, prev = last_two[0], last_two[1]
            name = app.name or app.store_app_id
            for draft in diff_snapshots(prev.raw, curr.raw, name):
                inserted = insert_event(
                    session,
                    event_type=draft.event_type,
                    event_date=curr.snapshot_date,
                    title=draft.title,
                    details=draft.details,
                    dedupe_key=f"{draft.event_type}|{app.id}|{curr.snapshot_date}",
                    app_id=app.id,
                    store=app.store,
                    store_app_id=app.store_app_id,
                )
                if inserted:
                    created += 1
                    log.info("event: %s", draft.title)
    return created


def detect_rank_events() -> int:
    """Compare the two most recent chart days for every watched chart."""
    created = 0
    with SessionLocal() as session:
        watches = list(session.execute(
            select(RankingWatch).where(RankingWatch.is_active.is_(True))
        ).scalars())

    for watch in watches:
        with session_scope() as session:
            dates = [d for (d,) in session.execute(
                select(Ranking.date)
                .where(Ranking.store == watch.store,
                       Ranking.country == watch.country,
                       Ranking.category == watch.category)
                .distinct()
                .order_by(Ranking.date.desc())
                .limit(2)
            )]
            if len(dates) < 2:
                continue
            curr_date, prev_date = dates[0], dates[1]

            rows = list(session.execute(
                select(Ranking)
                .where(Ranking.store == watch.store,
                       Ranking.country == watch.country,
                       Ranking.category == watch.category,
                       Ranking.date.in_([curr_date, prev_date]))
            ).scalars())

            # resolve names for apps we track; untracked chart apps get their
            # public store URL so the digest links straight to them
            chart_app_ids = {r.store_app_id for r in rows}
            names = {
                a.store_app_id: a.name
                for a in session.execute(
                    select(App).where(App.store == watch.store,
                                      App.store_app_id.in_(chart_app_ids),
                                      App.name.is_not(None))
                ).scalars()
            }
            for app_id in chart_app_ids:
                if app_id not in names:
                    names[app_id] = store_url(watch.store, app_id)

            charts: dict[tuple[str, str], dict[str, dict[str, int]]] = {}
            for r in rows:
                slot = charts.setdefault((r.collection, r.platform), {"prev": {}, "curr": {}})
                side = "curr" if r.date == curr_date else "prev"
                slot[side][r.store_app_id] = r.rank

            for (collection, platform), sides in charts.items():
                label = f"{watch.store}/{watch.country}/{watch.category}/{collection}"
                if watch.store == STORE_IOS and platform != "all":
                    label += f" ({platform})"
                moves = analyze_rank_moves(sides["prev"], sides["curr"], label,
                                           names=names,
                                           entry_top=entry_top_for_collection(collection))
                for app_id, draft in moves:
                    inserted = insert_event(
                        session,
                        event_type=draft.event_type,
                        event_date=curr_date,
                        title=draft.title,
                        details={**draft.details, "chart": label},
                        dedupe_key=(f"{draft.event_type}|{watch.store}|{watch.country}|"
                                    f"{watch.category}|{collection}|{platform}|{app_id}|{curr_date}"),
                        store=watch.store,
                        store_app_id=app_id,
                    )
                    if inserted:
                        created += 1
                        log.info("event: %s", draft.title)
    return created


def main() -> int:
    utf8_console()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-snapshots", action="store_true",
                        help="skip snapshot diffing")
    parser.add_argument("--skip-rankings", action="store_true",
                        help="skip chart-movement detection")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    total = 0
    if not args.skip_snapshots:
        total += detect_snapshot_events()
    if not args.skip_rankings:
        total += detect_rank_events()
    log.info("Done: %d new event(s). Send them with: python notify.py", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
