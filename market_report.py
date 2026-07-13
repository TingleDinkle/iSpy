"""Market sizing job: pull aggregate metrics for every saved market segment
(POST /{store}/apps/summary — 1 credit per segment) and print a report with
period-over-period deltas.

Run weekly:
    python market_report.py
Manage segments:
    python manage.py add-segment "play-puzzle-100k" play --category GAME_PUZZLE --min-downloads 100000
    python manage.py list-segments
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

from sqlalchemy import select

from tracker import utf8_console
from tracker.api_client import AppstoreSpyClient, AppstoreSpyError
from tracker.db import SessionLocal, session_scope
from tracker.ingest import insert_event, upsert_market_snapshot
from tracker.models import MarketSegment, MarketSnapshot

log = logging.getLogger("market_report")


def _fmt(value, prev=None, money=False) -> str:
    if value is None:
        return "—"
    text = f"${value:,.0f}" if money else f"{value:,.0f}"
    if prev not in (None, 0):
        delta = (value - prev) / prev * 100
        text += f" ({delta:+.1f}%)"
    return text


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
        segments = list(session.execute(
            select(MarketSegment).where(MarketSegment.is_active.is_(True))
        ).scalars())
    if not segments:
        log.error("No segments defined. Add one: python manage.py add-segment ...")
        return 1

    client = AppstoreSpyClient()
    failures = 0
    print(f"\nMarket report — {today}\n")
    header = (f"{'segment':<28} {'apps live':>10} {'downloads/mo':>18} "
              f"{'revenue/mo':>18} {'installs/day':>15}")
    print(header)
    print("-" * len(header))

    for segment in segments:
        try:
            summary = client.get_summary(segment.store, segment.filter)
        except AppstoreSpyError as exc:
            log.error("%s failed: %s", segment.name, exc)
            failures += 1
            continue
        if not summary:
            log.warning("%s returned no data", segment.name)
            continue
        with session_scope() as session:
            prev = session.execute(
                select(MarketSnapshot)
                .where(MarketSnapshot.segment_id == segment.id,
                       MarketSnapshot.date < today)
                .order_by(MarketSnapshot.date.desc()).limit(1)
            ).scalar_one_or_none()
            seg = session.get(MarketSegment, segment.id)
            upsert_market_snapshot(session, seg, today, summary)
            print(f"{segment.name[:26]:<28} "
                  f"{_fmt(summary.get('available'), prev.available if prev else None):>10} "
                  f"{_fmt(summary.get('downloads'), prev.downloads if prev else None):>18} "
                  f"{_fmt(summary.get('revenue'), prev.revenue if prev else None, money=True):>18} "
                  f"{_fmt(summary.get('ipd'), prev.ipd if prev else None):>15}")
            # a 'market_pulse' event so the sizing reaches the Discord digest
            insert_event(
                session,
                event_type="market_pulse",
                event_date=today,
                title=(f"{segment.name}: "
                       f"{_fmt(summary.get('revenue'), prev.revenue if prev else None, money=True)} rev/mo, "
                       f"{_fmt(summary.get('downloads'), prev.downloads if prev else None)} dl/mo, "
                       f"{_fmt(summary.get('available'), prev.available if prev else None)} apps live"),
                details={"summary": summary,
                         "previous_date": prev.date.isoformat() if prev else None},
                dedupe_key=f"market_pulse|{segment.id}|{today}",
                store=segment.store,
            )

    print()
    log.info("Credits used this month: %d / %d",
             client.ledger.used_this_month, client.ledger.budget)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
