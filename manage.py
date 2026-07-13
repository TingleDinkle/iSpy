"""Management CLI: schema creation, registries (apps / studios / charts /
segments), tier control, seeding, and credit usage.

Usage:
    python manage.py init-db
    python manage.py seed                       # puzzle/casual starter pack
    python manage.py add-app ios 553834731 [--tier watch]
    python manage.py set-tier ios 553834731 watch
    python manage.py add-developer play 6577204690045492686 --name King
    python manage.py add-watch ios US GAMES_PUZZLE [--top 200]
    python manage.py add-segment play-puzzle-100k play --category GAME_PUZZLE --min-downloads 100000
    python manage.py list-apps | list-developers | list-watches | list-segments
    python manage.py credits
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
import sys
from pathlib import Path

from sqlalchemy import func, select, text

from tracker.api_client import (
    AppstoreSpyClient,
    AppstoreSpyError,
    AuthenticationError,
    RequestFailed,
    month_start_utc,
)
from tracker import utf8_console
from tracker.config import settings
from tracker.db import SessionLocal, engine, session_scope
from tracker.ingest import refresh_app_metadata
from tracker.models import (
    ApiRequest,
    App,
    Base,
    Developer,
    MarketSegment,
    RankingWatch,
    STORE_IOS,
    STORE_PLAY,
    TIER_PRIMARY,
    TIER_WATCH,
)

log = logging.getLogger("manage")

VIEWS_SQL = Path(__file__).resolve().parent / "sql" / "views.sql"


def cmd_init_db(_: argparse.Namespace) -> int:
    Base.metadata.create_all(engine)
    if VIEWS_SQL.exists():
        statements = [s.strip() for s in VIEWS_SQL.read_text(encoding="utf-8").split(";\n")
                      if s.strip()]
        with engine.begin() as conn:
            for statement in statements:
                conn.execute(text(statement))
        print(f"Schema + {len(statements)} dashboard view(s) created.")
    else:
        print("Schema created (sql/views.sql not found — views skipped).")
    return 0


def cmd_add_app(args: argparse.Namespace) -> int:
    country = (args.country or settings.default_country).upper()
    tier = getattr(args, "tier", None) or TIER_PRIMARY
    with session_scope() as session:
        existing = session.execute(
            select(App).where(App.store == args.store, App.store_app_id == args.app_id)
        ).scalar_one_or_none()
        if existing:
            existing.is_active = True
            existing.country = country
            existing.tier = tier
            print(f"{args.store}:{args.app_id} already tracked — reactivated ({tier}).")
            return 0
        app = App(store=args.store, store_app_id=args.app_id, country=country, tier=tier)
        session.add(app)

    def untrack() -> None:
        with session_scope() as session:
            bad = session.execute(
                select(App).where(App.store == args.store, App.store_app_id == args.app_id)
            ).scalar_one_or_none()
            if bad is not None:
                session.delete(bad)

    if not args.no_validate:
        # One credit: confirm the id exists and grab its metadata right away.
        try:
            payload = AppstoreSpyClient().get_app(args.store, args.app_id, country=country)
        except (AuthenticationError, RequestFailed) as exc:
            # Deterministic failure (bad key, bad id, bad params) — retrying
            # nightly would just burn credits, so don't keep the app.
            untrack()
            print(f"ERROR: validation failed ({exc}). App NOT tracked.")
            return 1
        except AppstoreSpyError as exc:
            print(f"WARNING: transient validation failure ({exc}). "
                  f"App stored anyway; first snapshot will retry.")
            return 0
        if payload is None:
            print("App not in AppstoreSpy's database yet — it was queued for "
                  "crawling (HTTP 202). Data should appear within a day or two.")
            return 0
        with session_scope() as session:
            app = session.execute(
                select(App).where(App.store == args.store, App.store_app_id == args.app_id)
            ).scalar_one()
            refresh_app_metadata(app, payload)
            print(f"Tracking {args.store}:{args.app_id} — {app.name!r} by {app.developer_name!r}")
        return 0

    print(f"Tracking {args.store}:{args.app_id} (not validated).")
    return 0


def cmd_deactivate_app(args: argparse.Namespace) -> int:
    with session_scope() as session:
        app = session.execute(
            select(App).where(App.store == args.store, App.store_app_id == args.app_id)
        ).scalar_one_or_none()
        if app is None:
            print(f"{args.store}:{args.app_id} is not tracked.")
            return 1
        app.is_active = False
    print(f"Deactivated {args.store}:{args.app_id}. History is retained.")
    return 0


def cmd_list_apps(_: argparse.Namespace) -> int:
    with SessionLocal() as session:
        apps = list(session.execute(select(App).order_by(App.store, App.id)).scalars())
    if not apps:
        print("No apps tracked yet. Add one: python manage.py add-app ios 553834731")
        return 0
    header = (f"{'store':<6} {'app id':<40} {'name':<32} {'country':<7} "
              f"{'tier':<8} {'active':<6}")
    print(header)
    print("-" * len(header))
    for a in apps:
        print(f"{a.store:<6} {a.store_app_id:<40} {(a.name or '?')[:32]:<32} "
              f"{a.country:<7} {a.tier:<8} {'yes' if a.is_active else 'no':<6}")
    return 0


def cmd_set_tier(args: argparse.Namespace) -> int:
    with session_scope() as session:
        app = session.execute(
            select(App).where(App.store == args.store, App.store_app_id == args.app_id)
        ).scalar_one_or_none()
        if app is None:
            print(f"{args.store}:{args.app_id} is not tracked.")
            return 1
        app.tier = args.tier
    print(f"{args.store}:{args.app_id} -> {args.tier} tier.")
    return 0


def cmd_add_developer(args: argparse.Namespace) -> int:
    name = args.name
    if not args.no_validate:
        try:
            payload = AppstoreSpyClient().get_developer(args.store, args.dev_id)
            if payload:
                name = payload.get("name") or name
        except (AuthenticationError, RequestFailed) as exc:
            print(f"ERROR: developer lookup failed ({exc}). Not added.")
            return 1
        except AppstoreSpyError as exc:
            print(f"WARNING: transient lookup failure ({exc}); storing without name check.")
    with session_scope() as session:
        existing = session.execute(
            select(Developer).where(Developer.store == args.store,
                                    Developer.store_dev_id == args.dev_id)
        ).scalar_one_or_none()
        if existing:
            existing.is_active = True
            existing.auto_track = not args.no_auto_track
            existing.name = name or existing.name
            print(f"Developer {existing.name or args.dev_id} reactivated.")
            return 0
        session.add(Developer(store=args.store, store_dev_id=args.dev_id, name=name,
                              auto_track=not args.no_auto_track))
    print(f"Watching studio {name or args.dev_id} ({args.store}). "
          f"New games will appear in the events feed"
          f"{' and be auto-tracked' if not args.no_auto_track else ''}.")
    return 0


def cmd_list_developers(_: argparse.Namespace) -> int:
    with SessionLocal() as session:
        devs = list(session.execute(select(Developer).order_by(Developer.store)).scalars())
    if not devs:
        print("No studios watched. Add one: python manage.py add-developer <store> <dev_id>")
        return 0
    header = f"{'store':<6} {'developer id':<26} {'name':<28} {'auto-track':<10} {'active':<6}"
    print(header)
    print("-" * len(header))
    for d in devs:
        print(f"{d.store:<6} {d.store_dev_id:<26} {(d.name or '?')[:28]:<28} "
              f"{'yes' if d.auto_track else 'no':<10} {'yes' if d.is_active else 'no':<6}")
    return 0


def cmd_add_watch(args: argparse.Namespace) -> int:
    with session_scope() as session:
        existing = session.execute(
            select(RankingWatch).where(RankingWatch.store == args.store,
                                       RankingWatch.country == args.country.upper(),
                                       RankingWatch.category == args.category)
        ).scalar_one_or_none()
        if existing:
            existing.is_active = True
            existing.rank_depth = args.top
            print(f"Watch {args.store}/{args.country}/{args.category} updated (top {args.top}).")
            return 0
        session.add(RankingWatch(store=args.store, country=args.country.upper(),
                                 category=args.category, rank_depth=args.top))
    print(f"Watching chart {args.store}/{args.country.upper()}/{args.category} "
          f"(top {args.top}, all collections).")
    return 0


def cmd_list_watches(_: argparse.Namespace) -> int:
    with SessionLocal() as session:
        watches = list(session.execute(select(RankingWatch)).scalars())
    if not watches:
        print("No chart watches. Add one: python manage.py add-watch ios US GAMES_PUZZLE")
        return 0
    for w in watches:
        state = "active" if w.is_active else "inactive"
        print(f"  {w.store}/{w.country}/{w.category} — top {w.rank_depth} ({state})")
    return 0


def cmd_add_segment(args: argparse.Namespace) -> int:
    if args.filter_json:
        filter_body = json.loads(args.filter_json)
    else:
        filter_body = {}
        if args.category:
            filter_body["category"] = args.category
        if args.min_downloads:
            filter_body["downloads_month"] = {"from": args.min_downloads}
        if args.min_revenue:
            filter_body["revenue_month"] = {"from": args.min_revenue}
    if not filter_body:
        print("Empty filter — pass --category / --min-downloads / --min-revenue "
              "or --filter-json.")
        return 1
    with session_scope() as session:
        existing = session.execute(
            select(MarketSegment).where(MarketSegment.name == args.name)
        ).scalar_one_or_none()
        if existing:
            existing.filter = filter_body
            existing.store = args.store
            existing.is_active = True
            print(f"Segment '{args.name}' updated: {json.dumps(filter_body)}")
            return 0
        session.add(MarketSegment(name=args.name, store=args.store, filter=filter_body))
    print(f"Segment '{args.name}' ({args.store}): {json.dumps(filter_body)}")
    return 0


def cmd_list_segments(_: argparse.Namespace) -> int:
    with SessionLocal() as session:
        segments = list(session.execute(select(MarketSegment)).scalars())
    if not segments:
        print("No segments. Add one: python manage.py add-segment play-puzzle-100k play "
              "--category GAME_PUZZLE --min-downloads 100000")
        return 0
    for s in segments:
        state = "active" if s.is_active else "inactive"
        print(f"  {s.name} ({s.store}, {state}): {json.dumps(s.filter)}")
    return 0


# Puzzle/casual starter pack: resolved against the live API at seed time so
# no store id is hardcoded. (game title, expected studio substring)
SEED_GAMES: list[tuple[str, str]] = [
    ("Candy Crush Saga", "king"),
    ("Royal Match", "dream"),
    ("Gardenscapes", "playrix"),
    ("Homescapes", "playrix"),
    ("Merge Mansion", "metacore"),
    ("Gossip Harbor", "microfun"),
    ("Block Blast", "hungry"),
    ("Coin Master", "moon active"),
]

SEED_WATCHES = [
    (STORE_IOS, "US", "GAMES_PUZZLE"),
    (STORE_PLAY, "US", "GAME_PUZZLE"),
    (STORE_PLAY, "US", "GAME_CASUAL"),
]

SEED_SEGMENTS = [
    ("play-puzzle-100kdl", STORE_PLAY, {"category": "GAME_PUZZLE",
                                        "downloads_month": {"from": 100_000}}),
    ("play-casual-100kdl", STORE_PLAY, {"category": "GAME_CASUAL",
                                        "downloads_month": {"from": 100_000}}),
    ("ios-puzzle-100krev", STORE_IOS, {"category": "GAMES_PUZZLE",
                                       "revenue_month": {"from": 100_000}}),
]


def cmd_seed(args: argparse.Namespace) -> int:
    """Seed puzzle/casual genre leaders, chart watches, and market segments.
    Costs ~2 credits per game (search on each store) — ~35 credits total."""
    if not args.yes:
        print("This will call the API (~35 credits) to resolve and track "
              f"{len(SEED_GAMES)} games on both stores, plus {len(SEED_WATCHES)} "
              f"chart watches and {len(SEED_SEGMENTS)} market segments.")
        print("Re-run with --yes to proceed.")
        return 1

    client = AppstoreSpyClient()
    added_apps = added_devs = 0
    for title, studio_hint in SEED_GAMES:
        for store in (STORE_IOS, STORE_PLAY):
            try:
                # Stores are full of copycat clones; the genuine title has
                # orders of magnitude more downloads, so rank by that and
                # demand the studio name corroborates. (The GET search
                # endpoint rejects this sort — POST /apps/query supports it.)
                hits = client.query_apps(
                    store, {"name": title}, limit=5, sort="-downloads_month",
                    fields=["id", "name", "developer_id", "developer_name",
                            "downloads_month"],
                )
            except AppstoreSpyError as exc:
                print(f"  search '{title}' ({store}) failed: {exc}")
                continue

            def _norm(text: str | None) -> str:
                # store listings love unicode variants ('Block\xa0Blast！') —
                # compare on alphanumerics only
                return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()

            def genuine(candidate: dict) -> bool:
                name = _norm(candidate.get("name"))
                dev = _norm(candidate.get("developer_name"))
                return _norm(title) in name and _norm(studio_hint) in dev

            hit = next((h for h in hits if genuine(h)), None)
            if hit is None:
                best = hits[0] if hits else None
                print(f"  '{title}' ({store}): no hit matched studio "
                      f"'{studio_hint}' (best: {best.get('name') if best else '—'!r} "
                      f"by {best.get('developer_name') if best else '—'!r}) — "
                      f"skipped, add manually")
                continue
            with session_scope() as session:
                app = session.execute(
                    select(App).where(App.store == store,
                                      App.store_app_id == str(hit["id"]))
                ).scalar_one_or_none()
                if app is None:
                    session.add(App(store=store, store_app_id=str(hit["id"]),
                                    name=hit.get("name"),
                                    developer_name=hit.get("developer_name"),
                                    tier=TIER_PRIMARY))
                    added_apps += 1
                    print(f"  tracking {hit.get('name')} ({store}) — "
                          f"{hit.get('developer_name')}")
                dev_id = hit.get("developer_id")
                if dev_id:
                    dev = session.execute(
                        select(Developer).where(Developer.store == store,
                                                Developer.store_dev_id == str(dev_id))
                    ).scalar_one_or_none()
                    if dev is None:
                        session.add(Developer(store=store, store_dev_id=str(dev_id),
                                              name=hit.get("developer_name"),
                                              auto_track=True))
                        added_devs += 1

    for store, country, category in SEED_WATCHES:
        cmd_add_watch(argparse.Namespace(store=store, country=country,
                                         category=category,
                                         top=settings.rank_track_depth))
    for name, store, filter_body in SEED_SEGMENTS:
        cmd_add_segment(argparse.Namespace(name=name, store=store, filter_json=None,
                                           category=filter_body.get("category"),
                                           min_downloads=(filter_body.get("downloads_month")
                                                          or {}).get("from"),
                                           min_revenue=(filter_body.get("revenue_month")
                                                        or {}).get("from")))

    print(f"\nSeeded {added_apps} app(s) and {added_devs} studio watch(es).")
    print("Next: python daily_snapshot.py")
    return 0


def cmd_credits(_: argparse.Namespace) -> int:
    start = month_start_utc()
    with SessionLocal() as session:
        used = session.execute(
            select(func.count())
            .select_from(ApiRequest)
            .where(ApiRequest.called_at >= start,
                   ApiRequest.status_code.between(200, 299))
        ).scalar_one()
    budget = settings.monthly_credit_budget
    now = dt.datetime.now(dt.timezone.utc)
    days_elapsed = max((now - start).days, 1)
    print(f"Month-to-date credits: {used:,} / {budget:,} ({used / budget:.1%})")
    print(f"Daily average:         {used / days_elapsed:,.0f}")
    return 0


def main() -> int:
    utf8_console()
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create all tables").set_defaults(func=cmd_init_db)

    p = sub.add_parser("add-app", help="track a new app")
    p.add_argument("store", choices=[STORE_IOS, STORE_PLAY])
    p.add_argument("app_id", help="Apple numeric id or Play bundle id")
    p.add_argument("--country", help=f"storefront (default {settings.default_country})")
    p.add_argument("--tier", choices=[TIER_PRIMARY, TIER_WATCH], default=TIER_PRIMARY,
                   help="primary = full daily pull; watch = weekly light pull")
    p.add_argument("--no-validate", action="store_true",
                   help="skip the 1-credit validation API call")
    p.set_defaults(func=cmd_add_app)

    p = sub.add_parser("set-tier", help="move an app between tiers")
    p.add_argument("store", choices=[STORE_IOS, STORE_PLAY])
    p.add_argument("app_id")
    p.add_argument("tier", choices=[TIER_PRIMARY, TIER_WATCH])
    p.set_defaults(func=cmd_set_tier)

    p = sub.add_parser("deactivate-app", help="stop tracking (keeps history)")
    p.add_argument("store", choices=[STORE_IOS, STORE_PLAY])
    p.add_argument("app_id")
    p.set_defaults(func=cmd_deactivate_app)

    p = sub.add_parser("add-developer", help="watch a studio for new games")
    p.add_argument("store", choices=[STORE_IOS, STORE_PLAY])
    p.add_argument("dev_id", help="store developer id (see apps.developer_id)")
    p.add_argument("--name", help="display name")
    p.add_argument("--no-auto-track", action="store_true",
                   help="report new games but don't auto-add them to the registry")
    p.add_argument("--no-validate", action="store_true")
    p.set_defaults(func=cmd_add_developer)

    p = sub.add_parser("add-watch", help="watch a category chart daily")
    p.add_argument("store", choices=[STORE_IOS, STORE_PLAY])
    p.add_argument("country", help="e.g. US")
    p.add_argument("category", help="iOS: GAMES_PUZZLE...; Play: GAME_PUZZLE...")
    p.add_argument("--top", type=int, default=settings.rank_track_depth,
                   help="chart depth to store (default %(default)s)")
    p.set_defaults(func=cmd_add_watch)

    p = sub.add_parser("add-segment", help="save a market filter for market_report.py")
    p.add_argument("name")
    p.add_argument("store", choices=[STORE_IOS, STORE_PLAY])
    p.add_argument("--category")
    p.add_argument("--min-downloads", type=int)
    p.add_argument("--min-revenue", type=int)
    p.add_argument("--filter-json", help="raw SearchFilter JSON (overrides the flags)")
    p.set_defaults(func=cmd_add_segment)

    p = sub.add_parser("seed", help="seed puzzle/casual genre leaders + watches + segments")
    p.add_argument("--yes", action="store_true", help="confirm (~35 API credits)")
    p.set_defaults(func=cmd_seed)

    sub.add_parser("list-apps", help="show the registry").set_defaults(func=cmd_list_apps)
    sub.add_parser("list-developers", help="watched studios").set_defaults(func=cmd_list_developers)
    sub.add_parser("list-watches", help="watched charts").set_defaults(func=cmd_list_watches)
    sub.add_parser("list-segments", help="market segments").set_defaults(func=cmd_list_segments)
    sub.add_parser("credits", help="month-to-date credit usage").set_defaults(func=cmd_credits)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
