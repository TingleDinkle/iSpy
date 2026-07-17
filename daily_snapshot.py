"""Daily snapshot job: pull metadata, estimates, installs, and reviews for
every active tracked app and upsert them into PostgreSQL.

Idempotent — safe to re-run for the same day (upserts throughout). Failures
are isolated per app/stage so one bad app never sinks the whole run; the exit
code is non-zero if anything failed.

Usage:
    python daily_snapshot.py                     # everything, both stores
    python daily_snapshot.py --store ios         # one store only
    python daily_snapshot.py --skip-reviews      # metrics only
    python daily_snapshot.py --skip-estimates --skip-installs

Schedule daily via cron / Windows Task Scheduler after midnight UTC.
"""

from __future__ import annotations

import datetime as dt
import logging
import sys

from sqlalchemy import delete, func, select

from tracker.api_client import (
    AppstoreSpyClient,
    AppstoreSpyError,
    AuthenticationError,
    CreditBudgetExhausted,
    RequestFailed,
    RetriesExhausted,
)
from tracker.cli import build_parser, init_script
from tracker.config import settings
from tracker.db import SessionLocal, load_active, session_scope
from tracker.events import looks_like_soft_launch
from tracker.ingest import (
    insert_event,
    insert_reviews,
    refresh_app_metadata,
    sync_developer_apps,
    upsert_daily_installs,
    upsert_developer_estimates,
    upsert_monthly_estimates,
    upsert_rankings,
    upsert_snapshot,
)
from tracker.models import (
    App,
    AppSnapshot,
    DailyInstalls,
    Developer,
    DeveloperApp,
    DeveloperEstimate,
    MonthlyEstimate,
    RankingWatch,
    STORE_IOS,
    STORE_PLAY,
    TIER_PRIMARY,
    TIER_WATCH,
)

log = logging.getLogger("daily_snapshot")

APP_DETAIL_FIELDS = (
    "id", "bundle", "name", "category", "developer_id", "developer_name",
    "rating_value", "rating_count", "revenue", "downloads", "version",
    "released", "updated", "removed",
    # intelligence fields diffed by detect_events.py:
    "whatsnew", "icon", "screenshots", "countries_list", "advertised",
    "top_countries_revenue", "top_countries_downloads",  # geo revenue mix
    "description",           # ASO/listing rewrites
    "ads",                    # UA creative URLs (iOS object; Play boolean)
    "languages",              # localization coverage (archived in raw)
    "transferred", "previous_developer_id", "previous_developer_name",
)


def snapshot_app_details(client: AppstoreSpyClient, app: App, today: dt.date) -> bool:
    """Fetch app details and store today's snapshot. True on success."""
    payload = client.get_app(app.store, app.store_app_id, country=app.country,
                             fields=APP_DETAIL_FIELDS)
    if payload is None:
        log.warning("%s:%s has no data yet (queued for crawl?) — skipped",
                    app.store, app.store_app_id)
        return False
    with session_scope() as session:
        db_app = session.get(App, app.id)
        refresh_app_metadata(db_app, payload)
        upsert_snapshot(session, db_app, payload, today)
    return True


def snapshot_estimates(client: AppstoreSpyClient, apps: list[App], today: dt.date) -> int:
    """Batched monthly estimates for all apps of one store, refreshed weekly.

    The data has month granularity (revised retroactively for ~4 months), so
    daily pulls buy nothing — at fleet scale the weekly gate saves ~1k
    credits/month. The full lookback window is kept on every pull, so
    upstream revisions still land within estimates_refresh_days.
    """
    if not apps:
        return 0
    store = apps[0].store
    with SessionLocal() as session:
        last_fetch = session.execute(
            select(func.max(MonthlyEstimate.fetched_at))
            .join(App, App.id == MonthlyEstimate.app_id)
            .where(App.store == store)
        ).scalar_one()
        covered = {
            app_id for (app_id,) in session.execute(
                select(MonthlyEstimate.app_id.distinct())
                .where(MonthlyEstimate.app_id.in_([a.id for a in apps]))
            )
        }
    new_apps = [a for a in apps if a.id not in covered]
    if last_fetch is not None and not new_apps:
        age = dt.datetime.now(dt.timezone.utc) - last_fetch
        if age.days < settings.estimates_refresh_days:
            log.info("%s estimates fresh (%dd old) — skipping until day %d",
                     store, age.days, settings.estimates_refresh_days)
            return 0
    start = today - dt.timedelta(days=settings.estimates_lookback_days)
    rows = client.get_estimates(store, [a.store_app_id for a in apps], start=start, end=today)
    id_map = {a.store_app_id: a.id for a in apps}
    with session_scope() as session:
        return upsert_monthly_estimates(session, id_map, rows)


def snapshot_daily_installs(client: AppstoreSpyClient, app: App, today: dt.date) -> int:
    """Play-only daily installs, resuming from the last stored date."""
    with SessionLocal() as session:
        last = session.execute(
            select(func.max(DailyInstalls.date)).where(DailyInstalls.app_id == app.id)
        ).scalar_one()
    if last is None:
        start = today - dt.timedelta(days=settings.installs_lookback_days)
    else:
        # Re-pull a few trailing days: upstream revises recent values.
        start = last - dt.timedelta(days=settings.installs_refetch_days)
    rows = client.get_daily_installs(app.store_app_id, start=start, end=today)
    with session_scope() as session:
        return upsert_daily_installs(session, app, rows)


def snapshot_reviews(client: AppstoreSpyClient, app: App) -> int:
    rows = client.get_reviews(app.store, app.store_app_id, country=app.country)
    with session_scope() as session:
        return insert_reviews(session, app, rows)


def snapshot_rankings(client: AppstoreSpyClient, watch: RankingWatch,
                      today: dt.date) -> int:
    """Pull the last few days of one watched chart (all collections)."""
    start = today - dt.timedelta(days=settings.rankings_lookback_days)
    rows = client.get_rankings(watch.store, watch.country, watch.category,
                               date_start=start, date_end=today,
                               rank_end=watch.rank_depth)
    with session_scope() as session:
        return upsert_rankings(session, watch.store, rows)


def snapshot_developer_estimates(client: AppstoreSpyClient, dev: Developer,
                                 today: dt.date) -> int:
    """Studio-level monthly revenue/downloads, refreshed weekly (estimates
    are monthly — pulling daily would waste a credit per studio per day)."""
    with SessionLocal() as session:
        last_fetch = session.execute(
            select(func.max(DeveloperEstimate.fetched_at))
            .where(DeveloperEstimate.developer_id == dev.id)
        ).scalar_one()
    if last_fetch is not None:
        age = dt.datetime.now(dt.timezone.utc) - last_fetch
        if age.days < settings.developer_estimates_refresh_days:
            return 0
    start = today - dt.timedelta(days=400)
    rows = client.get_developer_estimates(dev.store, dev.store_dev_id,
                                          start=start, end=today)
    with session_scope() as session:
        db_dev = session.get(Developer, dev.id)
        return upsert_developer_estimates(session, db_dev, rows)


def discover_developer_apps(client: AppstoreSpyClient, dev: Developer,
                            today: dt.date) -> tuple[int, int]:
    """Re-query a studio's portfolio; new apps become events (new-game radar).

    Returns (new_apps, events_created). The first sync for a developer seeds
    the baseline silently. New apps get one extra details call to check for a
    soft launch and, when auto_track is on, join the registry as 'watch' tier.
    """
    # Full portfolio, paginated: a single page truncates prolific publishers
    # and later misreports their old titles as "new games".
    rows = client.query_apps_all(
        dev.store, {"developer_id": dev.store_dev_id},
        fields=["id", "name", "release_date"], sort="-release_date",
    )
    with session_scope() as session:
        known_before = session.execute(
            select(func.count()).select_from(DeveloperApp)
            .where(DeveloperApp.developer_id == dev.id)
        ).scalar_one()
        db_dev = session.get(Developer, dev.id)
        new_apps = sync_developer_apps(session, db_dev, rows, today)
        new_specs = [(a.store_app_id, a.name, a.release_date) for a in new_apps]

    if known_before == 0:
        log.info("%s (%s): baseline of %d app(s) recorded", dev.name, dev.store,
                 len(new_specs))
        return len(new_specs), 0

    events = 0
    for store_app_id, name, release_date in new_specs:
        label = name or store_app_id
        # An "unseen" app released long ago is an upstream data correction
        # sliding into view, not a new game — baseline it silently.
        if release_date and (today - release_date).days > 365:
            log.info("%s: %s released %s — recorded without a new-game event",
                     dev.name, label, release_date)
            continue

        details = None
        retry_countries = False  # re-check tomorrow when data may still arrive
        try:
            details = client.get_app(dev.store, store_app_id,
                                     fields=("id", "name", "released",
                                             "countries_list", "version"))
            if details is None:
                # 202/204 on the default (US) storefront — soft-launched
                # titles usually have no US data, so try a test market.
                details = client.get_app(dev.store, store_app_id, country="PH",
                                         fields=("id", "name", "released",
                                                 "countries_list", "version"))
            retry_countries = details is None  # still queued for crawling
        except RetriesExhausted as exc:
            retry_countries = True  # transient — worth another attempt tomorrow
            log.warning("details for new app %s failed: %s", store_app_id, exc)
        except RequestFailed as exc:
            # deterministic 4xx — retrying daily would only burn credits;
            # fatal errors (auth, credit budget) propagate to main()'s handlers
            log.warning("details for new app %s rejected: %s", store_app_id, exc)

        countries = (details or {}).get("countries_list")
        with session_scope() as session:
            if insert_event(
                session,
                event_type="new_developer_app",
                event_date=today,
                title=f"{dev.name or dev.store_dev_id} has a new game: {label} ({dev.store})",
                details={"developer": dev.name, "app": store_app_id,
                         "countries": countries},
                dedupe_key=f"new_developer_app|{dev.store}|{store_app_id}",
                store=dev.store, store_app_id=store_app_id,
            ):
                events += 1
            if countries and looks_like_soft_launch(countries):
                if insert_event(
                    session,
                    event_type="soft_launch_detected",
                    event_date=today,
                    title=(f"Possible SOFT LAUNCH: {label} by "
                           f"{dev.name or dev.store_dev_id} is live in only "
                           f"{len(countries)} market(s): {', '.join(sorted(countries)[:8])}"),
                    details={"countries": sorted(countries), "app": store_app_id},
                    dedupe_key=f"soft_launch_detected|{dev.store}|{store_app_id}",
                    store=dev.store, store_app_id=store_app_id,
                ):
                    events += 1
            if dev.auto_track:
                exists = session.execute(
                    select(App).where(App.store == dev.store,
                                      App.store_app_id == store_app_id)
                ).scalar_one_or_none()
                if exists is None:
                    session.add(App(store=dev.store, store_app_id=store_app_id,
                                    name=name, tier=TIER_WATCH,
                                    country=settings.default_country))
                    log.info("auto-tracking new app %s (%s) as watch tier",
                             label, dev.store)
            if countries is None and retry_countries:
                # Not crawled yet (202/204) or transient failure: drop the
                # baseline row so tomorrow's run re-checks for a soft launch.
                # Events are deduped by key, so this produces no repeats.
                session.execute(
                    delete(DeveloperApp).where(
                        DeveloperApp.developer_id == dev.id,
                        DeveloperApp.store_app_id == store_app_id)
                )
                log.info("%s: countries unknown for %s — will re-check on the "
                         "next run", dev.name, label)
    return len(new_specs), events


def main() -> int:
    parser = build_parser(__doc__)
    parser.add_argument("--store", choices=[STORE_IOS, STORE_PLAY],
                        help="limit to one store")
    parser.add_argument("--tier", choices=[TIER_PRIMARY, TIER_WATCH, "all"],
                        default=None,
                        help="force a tier (default: primary daily, watch when stale)")
    parser.add_argument("--skip-reviews", action="store_true")
    parser.add_argument("--skip-estimates", action="store_true")
    parser.add_argument("--skip-installs", action="store_true")
    parser.add_argument("--skip-rankings", action="store_true")
    parser.add_argument("--skip-developers", action="store_true")
    parser.add_argument("--resume", action="store_true",
                        help="skip apps already snapshotted today (cheap crash "
                             "recovery: only the remaining apps buy credits)")
    args = parser.parse_args()
    init_script(args)

    today = dt.datetime.now(dt.timezone.utc).date()

    all_apps = load_active(App, store=args.store)
    with SessionLocal() as session:
        last_snapshot = dict(session.execute(
            select(AppSnapshot.app_id, func.max(AppSnapshot.snapshot_date))
            .group_by(AppSnapshot.app_id)
        ).all())

    # Tier scheduling: primary apps every run; watch apps only when their
    # last snapshot is at least watch_refresh_days old (self-healing, so a
    # missed cron day doesn't skip a week).
    def is_due(app: App) -> bool:
        if args.resume and last_snapshot.get(app.id) == today:
            return False  # already bought today's data before the crash
        if args.tier == "all":
            return True
        if args.tier is not None:
            return app.tier == args.tier
        if app.tier == TIER_PRIMARY:
            return True
        last = last_snapshot.get(app.id)
        return last is None or (today - last).days >= settings.watch_refresh_days

    apps = [a for a in all_apps if is_due(a)]
    skipped_watch = len(all_apps) - len(apps)

    if not apps and args.skip_rankings and args.skip_developers:
        if args.resume:
            log.info("Resume: everything already collected today — nothing left to do.")
            return 0
        log.error("Nothing to do. Add apps: python manage.py add-app <store> <id>")
        return 1

    log.info("Snapshotting %d app(s) for %s (%d watch-tier not due yet)",
             len(apps), today, skipped_watch)
    client = AppstoreSpyClient()
    failures: list[str] = []
    stats = {"snapshots": 0, "estimate_rows": 0, "install_rows": 0,
             "new_reviews": 0, "ranking_rows": 0, "new_dev_apps": 0,
             "dev_estimate_rows": 0, "events": 0}

    def run_stage(label: str, fn, *fn_args):
        """Run one collection stage with the fatal-vs-recoverable contract:
        AuthenticationError / CreditBudgetExhausted abort the whole run (they
        subclass AppstoreSpyError, so they MUST be re-raised before the
        generic handler); anything else is recorded and the run continues.
        Returns the stage result, or None on a recorded failure."""
        try:
            return fn(*fn_args)
        except (AuthenticationError, CreditBudgetExhausted):
            raise
        except AppstoreSpyError as exc:
            failures.append(f"{label}: {exc}")
            log.error("%s failed: %s", label, exc)
            return None

    try:
        # Per-app details + reviews + (Play) daily installs
        for app in apps:
            label = f"{app.store}:{app.store_app_id}"
            if run_stage(f"{label} details", snapshot_app_details, client, app, today):
                stats["snapshots"] += 1

            # installs and reviews are primary-tier only: they cost a credit
            # per app per day and watch apps only need the metric trail
            if (app.store == STORE_PLAY and not args.skip_installs
                    and app.tier == TIER_PRIMARY):
                rows = run_stage(f"{label} installs", snapshot_daily_installs,
                                 client, app, today)
                stats["install_rows"] += rows or 0

            if not args.skip_reviews and app.tier == TIER_PRIMARY:
                reviews = run_stage(f"{label} reviews", snapshot_reviews, client, app)
                stats["new_reviews"] += reviews or 0

        # Batched monthly estimates, once per store (weekly-gated inside)
        if not args.skip_estimates:
            for store in (STORE_IOS, STORE_PLAY):
                store_apps = [a for a in apps if a.store == store]
                if store_apps:
                    rows = run_stage(f"{store} estimates", snapshot_estimates,
                                     client, store_apps, today)
                    stats["estimate_rows"] += rows or 0

        # Top-chart rankings for every watched category chart
        if not args.skip_rankings:
            for watch in load_active(RankingWatch, store=args.store):
                chart = f"{watch.store}/{watch.country}/{watch.category}"
                rows = run_stage(f"rankings {chart}", snapshot_rankings,
                                 client, watch, today)
                if rows is not None:
                    stats["ranking_rows"] += rows
                    log.info("rankings %s: %d rows", chart, rows)

        # Studio portfolios: the new-game radar (+ weekly studio estimates)
        if not args.skip_developers:
            for dev in load_active(Developer, store=args.store):
                dev_label = f"developer {dev.name or dev.store_dev_id}"
                result = run_stage(dev_label, discover_developer_apps, client, dev, today)
                if result is not None:
                    new_apps, events = result
                    stats["new_dev_apps"] += new_apps
                    stats["events"] += events
                rows = run_stage(f"{dev_label} estimates",
                                 snapshot_developer_estimates, client, dev, today)
                stats["dev_estimate_rows"] += rows or 0

    except AuthenticationError as exc:
        log.critical("%s", exc)
        return 2
    except CreditBudgetExhausted as exc:
        log.critical("Stopping run: %s", exc)
        failures.append(str(exc))

    log.info(
        "Done. snapshots=%(snapshots)d estimate_rows=%(estimate_rows)d "
        "install_rows=%(install_rows)d new_reviews=%(new_reviews)d "
        "ranking_rows=%(ranking_rows)d new_dev_apps=%(new_dev_apps)d "
        "dev_estimate_rows=%(dev_estimate_rows)d events=%(events)d", stats,
    )
    log.info("Credits used this month: %d / %d",
             client.ledger.used_this_month, client.ledger.budget)

    if failures:
        log.warning("%d failure(s):", len(failures))
        for f in failures:
            log.warning("  - %s", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
