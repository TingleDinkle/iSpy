"""Review mining job (DB-only, zero API credits).

1. Tags untagged reviews with game-design topics (crash_bug, monetization,
   difficulty, progression_grind, ...) via tracker/review_topics.py rules.
2. Computes each app's helpfulness-weighted average stars over the last
   ``--window`` days vs the prior window; a drop >= ``--drop`` stars raises a
   'rating' alert.
3. Detects topic surges (e.g. crash complaints tripling week-over-week) and
   writes 'review_topic_surge' events with sample quotes.

Run daily after daily_snapshot.py:
    python analyze_reviews.py
    python analyze_reviews.py --retag        # re-tag ALL reviews after editing TOPICS
    python analyze_reviews.py --report       # print per-app topic breakdown
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from typing import Optional, Sequence

from sqlalchemy import null, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from tracker import utf8_console
from tracker.config import settings
from tracker.db import SessionLocal, session_scope
from tracker.ingest import insert_event
from tracker.models import Alert, App, Review
from tracker.review_topics import classify, review_weight

log = logging.getLogger("analyze_reviews")


def tag_reviews(batch_size: int = 2000) -> int:
    """Fill Review.topics for every untagged review, in batches. (--retag
    clears all tags first, so this single NULL-filtered path covers both.)"""
    tagged = 0
    while True:
        with session_scope() as session:
            rows = list(session.execute(
                select(Review.id, Review.title, Review.comment)
                .where(Review.topics.is_(None))
                .limit(batch_size)
            ))
            for review_id, title, comment in rows:
                text = " ".join(filter(None, [title, comment]))
                session.execute(
                    update(Review).where(Review.id == review_id)
                    .values(topics=classify(text))
                )
            tagged += len(rows)
        if len(rows) < batch_size:
            break
    return tagged


def bucket_for(created_at: dt.datetime, today: dt.date, window_days: int) -> Optional[str]:
    """Assign a review to the 'recent' or 'prior' window (or None).

    Both windows are exactly ``window_days`` calendar days in UTC:
    recent = (today - w, today], prior = (today - 2w, today - w]. Timestamps
    are normalised to UTC first — psycopg returns timestamptz in the
    connection's local timezone, which would misbucket reviews near midnight.
    """
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=dt.timezone.utc)
    day = created_at.astimezone(dt.timezone.utc).date()
    recent_cutoff = today - dt.timedelta(days=window_days)
    prior_cutoff = today - dt.timedelta(days=2 * window_days)
    if day > recent_cutoff:
        return "recent"
    if day > prior_cutoff:
        return "prior"
    return None


def weighted_avg_stars(rows: Sequence[tuple[Optional[int], Optional[int]]]) -> Optional[float]:
    """rows = (stars, likes). Returns helpfulness-weighted mean, or None."""
    total_weight = 0.0
    total = 0.0
    for stars, likes in rows:
        if stars is None:
            continue
        w = review_weight(likes)
        total += stars * w
        total_weight += w
    return (total / total_weight) if total_weight > 0 else None


def detect_rating_drops(window_days: int, drop_threshold: float,
                        min_reviews: int, today: dt.date) -> int:
    """Compare weighted avg stars: last window vs the window before it."""
    created = 0
    prior_cutoff = today - dt.timedelta(days=2 * window_days)

    with SessionLocal() as session:
        apps = list(session.execute(select(App).where(App.is_active.is_(True))).scalars())

    for app in apps:
        with session_scope() as session:
            rows = list(session.execute(
                select(Review.stars, Review.likes, Review.created_at)
                .where(Review.app_id == app.id,
                       Review.created_at.is_not(None),
                       Review.created_at > dt.datetime.combine(
                           prior_cutoff, dt.time.max, tzinfo=dt.timezone.utc))
            ))
            buckets = {"recent": [], "prior": []}
            for s, l, created_at in rows:
                bucket = bucket_for(created_at, today, window_days)
                if bucket:
                    buckets[bucket].append((s, l))
            recent, prior = buckets["recent"], buckets["prior"]
            if len(recent) < min_reviews or len(prior) < min_reviews:
                continue
            recent_avg = weighted_avg_stars(recent)
            prior_avg = weighted_avg_stars(prior)
            if recent_avg is None or prior_avg is None:
                continue
            if (prior_avg - recent_avg) >= drop_threshold:
                pct = (recent_avg - prior_avg) / prior_avg * 100.0
                stmt = pg_insert(Alert).values(
                    app_id=app.id, metric="rating", alert_date=today,
                    value=round(recent_avg, 2), baseline=round(prior_avg, 2),
                    pct_change=round(pct, 2), window_days=window_days,
                )
                result = session.execute(
                    stmt.on_conflict_do_nothing(constraint="uq_alerts_app_metric_date")
                    .returning(Alert.id)  # rowcount unreliable under psycopg3
                )
                if result.first() is not None:
                    created += 1
                    log.info("rating drop: %s %.2f -> %.2f stars (%d recent reviews)",
                             app.name or app.store_app_id, prior_avg, recent_avg,
                             len(recent))
    return created


def detect_topic_surges(window_days: int, today: dt.date) -> int:
    """Flag topics whose mention count jumped vs the prior window."""
    created = 0
    prior_cutoff = today - dt.timedelta(days=2 * window_days)

    with SessionLocal() as session:
        apps = list(session.execute(select(App).where(App.is_active.is_(True))).scalars())

    for app in apps:
        with session_scope() as session:
            rows = list(session.execute(
                select(Review.topics, Review.created_at, Review.comment)
                .where(Review.app_id == app.id,
                       Review.topics.is_not(None),
                       Review.created_at.is_not(None),
                       Review.created_at > dt.datetime.combine(
                           prior_cutoff, dt.time.max, tzinfo=dt.timezone.utc))
            ))
            recent_counts: dict[str, int] = {}
            prior_counts: dict[str, int] = {}
            samples: dict[str, list[str]] = {}
            for topics, created_at, comment in rows:
                which = bucket_for(created_at, today, window_days)
                if which is None:
                    continue
                bucket = recent_counts if which == "recent" else prior_counts
                for topic in topics or []:
                    if topic == "praise":
                        continue  # surges of praise are lovely but not actionable
                    bucket[topic] = bucket.get(topic, 0) + 1
                    if bucket is recent_counts and comment:
                        samples.setdefault(topic, [])
                        if len(samples[topic]) < 3:
                            samples[topic].append(comment[:200])

            name = app.name or app.store_app_id
            for topic, count in recent_counts.items():
                prior = prior_counts.get(topic, 0)
                if count >= settings.topic_surge_min and count >= settings.topic_surge_ratio * max(prior, 1):
                    inserted = insert_event(
                        session,
                        event_type="review_topic_surge",
                        event_date=today,
                        title=(f"{name}: '{topic}' complaints surging — {count} mentions "
                               f"in {window_days}d (was {prior})"),
                        details={"topic": topic, "recent": count, "prior": prior,
                                 "samples": samples.get(topic, [])},
                        dedupe_key=f"review_topic_surge|{app.id}|{topic}|{today}",
                        app_id=app.id, store=app.store, store_app_id=app.store_app_id,
                    )
                    if inserted:
                        created += 1
                        log.info("topic surge: %s / %s (%d vs %d)", name, topic, count, prior)
    return created


def print_report(window_days: int, today: dt.date) -> None:
    start = today - dt.timedelta(days=window_days)
    with SessionLocal() as session:
        apps = list(session.execute(select(App).where(App.is_active.is_(True))).scalars())
        print(f"\nReview topics, last {window_days} days (since {start}):\n")
        for app in apps:
            rows = list(session.execute(
                select(Review.topics, Review.stars)
                .where(Review.app_id == app.id,
                       Review.topics.is_not(None),
                       Review.created_at >= dt.datetime.combine(
                           start, dt.time.min, tzinfo=dt.timezone.utc))
            ))
            if not rows:
                continue
            counts: dict[str, int] = {}
            for topics, _ in rows:
                for t in topics or []:
                    counts[t] = counts.get(t, 0) + 1
            stars = [s for _, s in rows if s is not None]
            avg = sum(stars) / len(stars) if stars else 0
            top = ", ".join(f"{t}:{c}" for t, c in
                            sorted(counts.items(), key=lambda kv: -kv[1])[:5]) or "—"
            print(f"  {(app.name or app.store_app_id)[:36]:<38} "
                  f"{len(rows):>4} reviews  {avg:.2f}★  {top}")
        print()


def main() -> int:
    utf8_console()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--window", type=int, default=7, help="days (default 7)")
    parser.add_argument("--drop", type=float, default=settings.rating_drop_stars,
                        help="star drop that triggers an alert (default %(default)s)")
    parser.add_argument("--min-reviews", type=int, default=settings.rating_min_reviews)
    parser.add_argument("--retag", action="store_true",
                        help="re-tag ALL reviews (after editing TOPICS)")
    parser.add_argument("--report", action="store_true",
                        help="print per-app topic breakdown and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    today = dt.datetime.now(dt.timezone.utc).date()

    if args.report:
        print_report(args.window, today)
        return 0

    if args.retag:
        with session_scope() as session:
            # null() forces SQL NULL; a bare None would serialize as JSONB
            # 'null', which `topics IS NULL` never matches — the retag would
            # permanently orphan every review from the tagger.
            session.execute(update(Review).values(topics=null()))
        log.info("Cleared existing tags for retag")

    tagged = tag_reviews()
    drops = detect_rating_drops(args.window, args.drop, args.min_reviews, today)
    surges = detect_topic_surges(args.window, today)
    log.info("Done: tagged=%d rating_alerts=%d topic_surges=%d", tagged, drops, surges)
    return 0


if __name__ == "__main__":
    sys.exit(main())
