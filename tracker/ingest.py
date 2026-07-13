"""Idempotent ingestion helpers: API payloads -> PostgreSQL upserts.

Everything here uses INSERT ... ON CONFLICT so the daily job can be re-run
safely (crashes, backfills, revised upstream estimates).
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from .models import (
    App,
    AppEvent,
    AppSnapshot,
    DailyInstalls,
    Developer,
    DeveloperApp,
    DeveloperEstimate,
    MarketSegment,
    MarketSnapshot,
    MonthlyEstimate,
    Ranking,
    Review,
)

log = logging.getLogger(__name__)


def _parse_created(value: Optional[str]) -> Optional[dt.datetime]:
    """Parse review timestamps. iOS sends '+00:00' offsets; Play sends naive
    datetimes, which we treat as UTC."""
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        log.warning("Unparseable review timestamp: %r", value)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _as_category(value: Any) -> Optional[str]:
    """iOS returns a list of categories, Play a string; normalise to text."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return str(value)


def refresh_app_metadata(app: App, payload: dict[str, Any]) -> None:
    """Keep the registry row current with the latest crawl."""
    app.name = payload.get("name") or app.name
    app.bundle_id = payload.get("bundle") or app.bundle_id
    app.developer_name = payload.get("developer_name") or app.developer_name
    app.category = _as_category(payload.get("category")) or app.category


def upsert_snapshot(
    session: Session,
    app: App,
    payload: dict[str, Any],
    snapshot_date: dt.date,
) -> None:
    stmt = pg_insert(AppSnapshot).values(
        app_id=app.id,
        snapshot_date=snapshot_date,
        name=payload.get("name"),
        version=payload.get("version"),
        rating_value=payload.get("rating_value"),
        rating_count=payload.get("rating_count"),
        revenue_monthly_est=payload.get("revenue"),
        downloads_monthly_est=payload.get("downloads"),
        raw=payload,
    )
    update_cols = {
        c: stmt.excluded[c]
        for c in (
            "name", "version", "rating_value", "rating_count",
            "revenue_monthly_est", "downloads_monthly_est", "raw",
        )
    }
    session.execute(
        stmt.on_conflict_do_update(constraint="uq_snapshots_app_date", set_=update_cols)
    )


def upsert_monthly_estimates(
    session: Session,
    app_id_by_store_key: dict[str, int],
    rows: list[dict[str, Any]],
) -> int:
    """Upsert /estimates rows. ``month`` arrives as 'YYYY-MM'."""
    written = 0
    for row in rows:
        internal_id = app_id_by_store_key.get(str(row.get("id")))
        if internal_id is None:
            log.warning("Estimates row for untracked app id %r — skipped", row.get("id"))
            continue
        try:
            month = dt.datetime.strptime(row["month"], "%Y-%m").date()
        except (KeyError, ValueError):
            log.warning("Estimates row with bad month %r — skipped", row.get("month"))
            continue
        stmt = pg_insert(MonthlyEstimate).values(
            app_id=internal_id,
            month=month,
            revenue=row.get("revenue"),
            downloads=row.get("downloads"),
        )
        session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_estimates_app_month",
                set_={"revenue": stmt.excluded.revenue, "downloads": stmt.excluded.downloads},
            )
        )
        written += 1
    return written


def upsert_daily_installs(session: Session, app: App, rows: list[dict[str, Any]]) -> int:
    written = 0
    for row in rows:
        try:
            day = dt.date.fromisoformat(row["date"])
        except (KeyError, ValueError):
            log.warning("installs_daily row with bad date %r — skipped", row.get("date"))
            continue
        stmt = pg_insert(DailyInstalls).values(
            app_id=app.id,
            date=day,
            ipd=row.get("ipd"),
            installs_cumulative=row.get("installs"),
        )
        session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_installs_app_date",
                set_={"ipd": stmt.excluded.ipd,
                      "installs_cumulative": stmt.excluded.installs_cumulative},
            )
        )
        written += 1
    return written


def upsert_rankings(session: Session, store: str, rows: list[dict[str, Any]]) -> int:
    """Upsert chart rows from /{store}/rankings. The live feed contains
    duplicate rows, so we dedupe on the unique key first — a single INSERT
    ... ON CONFLICT statement is not allowed to touch the same row twice."""
    deduped: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        try:
            day = dt.date.fromisoformat(row["date"])
        except (KeyError, TypeError, ValueError):
            log.warning("Ranking row with bad date %r — skipped", row.get("date"))
            continue
        app_id = row.get("app")
        rank = row.get("rank")
        if not app_id or rank is None:
            continue
        platform = row.get("platform") or "all"
        key = (day, str(app_id), platform, row.get("country"),
               row.get("category"), row.get("collection"))
        deduped[key] = {
            "store": store,
            "date": day,
            "store_app_id": str(app_id),
            "platform": platform,
            "country": row.get("country"),
            "category": row.get("category"),
            "collection": row.get("collection"),
            "rank": int(rank),
        }
    values = list(deduped.values())
    for i in range(0, len(values), 500):
        chunk = values[i : i + 500]
        stmt = pg_insert(Ranking).values(chunk)
        session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_rankings_slot", set_={"rank": stmt.excluded.rank}
            )
        )
    return len(values)


def sync_developer_apps(
    session: Session,
    developer: Developer,
    rows: list[dict[str, Any]],
    today: dt.date,
) -> list[DeveloperApp]:
    """Record the studio's current portfolio; return the genuinely NEW rows.

    On the very first sync for a developer the whole portfolio is new — the
    caller should treat that as baseline seeding, not as events.
    """
    existing = {
        r
        for (r,) in session.execute(
            select(DeveloperApp.store_app_id).where(
                DeveloperApp.developer_id == developer.id
            )
        )
    }
    new_rows: list[DeveloperApp] = []
    for row in rows:
        app_id = row.get("id")
        if not app_id or str(app_id) in existing:
            continue
        release = None
        if row.get("release_date"):
            try:
                release = dt.date.fromisoformat(str(row["release_date"])[:10])
            except ValueError:
                pass
        dev_app = DeveloperApp(
            developer_id=developer.id,
            store_app_id=str(app_id),
            name=row.get("name"),
            release_date=release,
            first_seen=today,
            raw=row,
        )
        session.add(dev_app)
        existing.add(str(app_id))
        new_rows.append(dev_app)
    return new_rows


def upsert_developer_estimates(
    session: Session, developer: Developer, rows: list[dict[str, Any]]
) -> int:
    """Upsert studio-level monthly estimates. ``month`` arrives as 'YYYY-MM'."""
    written = 0
    for row in rows:
        try:
            month = dt.datetime.strptime(row["month"], "%Y-%m").date()
        except (KeyError, TypeError, ValueError):
            log.warning("Developer estimate with bad month %r — skipped", row.get("month"))
            continue
        stmt = pg_insert(DeveloperEstimate).values(
            developer_id=developer.id,
            month=month,
            revenue=row.get("revenue"),
            downloads=row.get("downloads"),
        )
        session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_dev_estimates",
                set_={"revenue": stmt.excluded.revenue,
                      "downloads": stmt.excluded.downloads,
                      "fetched_at": func.now()},
            )
        )
        written += 1
    return written


def insert_event(
    session: Session,
    *,
    event_type: str,
    event_date: dt.date,
    title: str,
    dedupe_key: str,
    details: Optional[dict[str, Any]] = None,
    app_id: Optional[int] = None,
    store: Optional[str] = None,
    store_app_id: Optional[str] = None,
) -> bool:
    """Insert an intelligence event; True if new, False if already recorded."""
    stmt = pg_insert(AppEvent).values(
        app_id=app_id,
        store=store,
        store_app_id=store_app_id,
        event_type=event_type,
        event_date=event_date,
        title=title,
        details=details,
        dedupe_key=dedupe_key[:512],
    )
    # RETURNING distinguishes insert (1 row) from conflict (0 rows); plain
    # rowcount is -1 under psycopg3 here and cannot be trusted.
    result = session.execute(
        stmt.on_conflict_do_nothing(constraint="uq_events_dedupe").returning(AppEvent.id)
    )
    return result.first() is not None


def upsert_market_snapshot(
    session: Session, segment: MarketSegment, day: dt.date, summary: dict[str, Any]
) -> None:
    stmt = pg_insert(MarketSnapshot).values(
        segment_id=segment.id,
        date=day,
        total=summary.get("total"),
        available=summary.get("available"),
        removed=summary.get("removed"),
        ipd=summary.get("ipd"),
        revenue=summary.get("revenue"),
        downloads=summary.get("downloads"),
    )
    update_cols = {c: stmt.excluded[c]
                   for c in ("total", "available", "removed", "ipd", "revenue", "downloads")}
    session.execute(
        stmt.on_conflict_do_update(constraint="uq_market_segment_date", set_=update_cols)
    )


def insert_reviews(session: Session, app: App, rows: list[dict[str, Any]]) -> int:
    """Append-only: existing (app, store_review_id) rows are left untouched."""
    inserted = 0
    for row in rows:
        review_id = row.get("id")
        if not review_id:
            continue
        stmt = pg_insert(Review).values(
            app_id=app.id,
            store_review_id=str(review_id),
            stars=row.get("stars"),
            title=row.get("title"),
            comment=row.get("comment"),
            author_name=row.get("author_name") or row.get("user_name"),
            country=row.get("country"),
            lang=row.get("lang"),
            app_version=row.get("version"),
            likes=row.get("likes"),
            created_at=_parse_created(row.get("created")),
            raw=row,
        )
        result = session.execute(
            stmt.on_conflict_do_nothing(constraint="uq_reviews_app_review")
            .returning(Review.id)  # rowcount is -1 under psycopg3 — count RETURNING rows
        )
        if result.first() is not None:
            inserted += 1
    return inserted
