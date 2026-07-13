"""Spike detection: flag apps whose metric jumped >= N% above their trailing
7-day moving average, and persist alerts.

Metrics:
    revenue    daily-sampled rolling monthly revenue estimate (app_snapshots)
    downloads  daily-sampled rolling monthly downloads estimate (app_snapshots)
    installs   Google Play true daily installs (daily_installs.ipd, cleaned)

The Play installs feed is dirty — zero-filled gaps, cumulative-counter dumps
(a day whose "ipd" equals the lifetime total), and negative resets. Those are
masked out before any baseline is computed, otherwise every artefact would
trip the detector.

Usage:
    python detect_spikes.py                          # revenue, latest day per app
    python detect_spikes.py --metric installs
    python detect_spikes.py --threshold 75 --window 14
    python detect_spikes.py --full-history           # scan all history (backfill)
    python detect_spikes.py --dry-run                # report only, write nothing
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from tracker import utf8_console
from tracker.config import settings
from tracker.db import engine, session_scope
from tracker.models import Alert, App, AppSnapshot, DailyInstalls

log = logging.getLogger("detect_spikes")

SNAPSHOT_METRICS = {"revenue": "revenue_monthly_est", "downloads": "downloads_monthly_est"}


def load_metric_frame(metric: str) -> pd.DataFrame:
    """Return columns [app_id, date, value] for the requested metric."""
    if metric in SNAPSHOT_METRICS:
        column = getattr(AppSnapshot, SNAPSHOT_METRICS[metric])
        query = (
            select(
                AppSnapshot.app_id,
                AppSnapshot.snapshot_date.label("date"),
                column.label("value"),
            )
            .join(App, App.id == AppSnapshot.app_id)
            .where(App.is_active.is_(True), column.is_not(None))
            .order_by(AppSnapshot.app_id, AppSnapshot.snapshot_date)
        )
        return pd.read_sql(query, engine)

    if metric == "installs":
        query = (
            select(
                DailyInstalls.app_id,
                DailyInstalls.date,
                DailyInstalls.ipd.label("value"),
                DailyInstalls.installs_cumulative,
            )
            .join(App, App.id == DailyInstalls.app_id)
            .where(App.is_active.is_(True))
            .order_by(DailyInstalls.app_id, DailyInstalls.date)
        )
        df = pd.read_sql(query, engine)
        return _clean_installs(df)

    raise ValueError(f"Unknown metric: {metric!r}")


def _clean_installs(df: pd.DataFrame, outlier_multiplier: float | None = None) -> pd.DataFrame:
    """Mask artefacts in the Play installs feed (observed in the live API):
    non-positive values (gaps/resets), days where ipd equals the lifetime
    cumulative counter (backfill dumps), and extreme outliers vs a rolling
    median.

    The outlier cutoff must sit far above genuine viral spikes (rarely more
    than ~50x the median) but below cumulative-dump artefacts (1000x+); the
    default comes from settings.installs_outlier_multiplier (100x).
    """
    if outlier_multiplier is None:
        outlier_multiplier = settings.installs_outlier_multiplier
    df = df.copy()
    df["value"] = df["value"].astype("float64")
    df.loc[df["value"] <= 0, "value"] = np.nan

    dump = (
        df["value"].notna()
        & df["installs_cumulative"].notna()
        & (df["value"] == df["installs_cumulative"].astype("float64"))
    )
    df.loc[dump, "value"] = np.nan

    cleaned_groups = []
    for _, group in df.groupby("app_id", sort=False):
        group = group.sort_values("date").copy()
        med = group["value"].rolling(15, min_periods=5).median()
        # Early rows have no rolling verdict yet — fall back to the group's
        # global median so a dump in an app's first days can't slip through
        # and poison every baseline that includes it.
        med = med.fillna(group["value"].median())
        outlier = (group["value"] > med * outlier_multiplier).fillna(False)
        group.loc[outlier, "value"] = np.nan
        cleaned_groups.append(group)
    out = pd.concat(cleaned_groups, ignore_index=True) if cleaned_groups else df
    return out.drop(columns=["installs_cumulative"])


def detect_spikes(
    df: pd.DataFrame,
    threshold_pct: float,
    window_days: int,
    min_periods: int,
    min_baseline: float,
    latest_only: bool,
) -> pd.DataFrame:
    """Return rows [app_id, date, value, baseline, pct_change] for spikes.

    Baseline = mean of the *prior* `window_days` calendar days (the current
    day never contaminates its own baseline). Series are re-indexed to daily
    frequency so gaps count as missing days, not compressed away.
    """
    # A window shorter than min_periods is a pandas ValueError; clamp so
    # e.g. --window 3 just means "3-day baseline, all 3 days required".
    min_periods = min(min_periods, window_days)

    spikes = []
    for app_id, group in df.groupby("app_id", sort=False):
        # normalize() guards against DateTime-typed inputs: a stray
        # time-of-day component would silently misalign the daily grid.
        idx = pd.DatetimeIndex(pd.to_datetime(group["date"])).normalize()
        s = pd.Series(group["value"].astype("float64").to_numpy(), index=idx).sort_index()
        s = s[~s.index.duplicated(keep="last")]
        if len(s) < min_periods + 1:
            continue
        s = s.asfreq("D")
        baseline = s.shift(1).rolling(window=window_days, min_periods=min_periods).mean()
        pct = (s - baseline) / baseline * 100.0

        candidates = pd.DataFrame({"value": s, "baseline": baseline, "pct_change": pct})
        candidates = candidates.dropna()
        candidates = candidates[
            (candidates["baseline"] > 0)  # a zero baseline yields inf % — never a real spike signal
            & (candidates["baseline"] >= min_baseline)
            & (candidates["pct_change"] >= threshold_pct)
        ]
        if latest_only and not candidates.empty:
            last_data_day = s.dropna().index.max()
            candidates = candidates[candidates.index == last_data_day]
        for day, row in candidates.iterrows():
            spikes.append(
                {
                    "app_id": app_id,
                    "date": day.date(),
                    "value": float(row["value"]),
                    "baseline": float(row["baseline"]),
                    "pct_change": float(row["pct_change"]),
                }
            )
    return pd.DataFrame(spikes, columns=["app_id", "date", "value", "baseline", "pct_change"])


def persist_alerts(spikes: pd.DataFrame, metric: str, window_days: int) -> int:
    """Insert alerts; duplicates for (app, metric, date) are ignored."""
    written = 0
    with session_scope() as session:
        for row in spikes.itertuples(index=False):
            stmt = pg_insert(Alert).values(
                app_id=int(row.app_id),
                metric=metric,
                alert_date=row.date,
                value=round(row.value, 2),
                baseline=round(row.baseline, 2),
                pct_change=round(row.pct_change, 2),
                window_days=window_days,
            )
            result = session.execute(
                stmt.on_conflict_do_nothing(constraint="uq_alerts_app_metric_date")
                .returning(Alert.id)  # rowcount unreliable under psycopg3
            )
            if result.first() is not None:
                written += 1
    return written


def print_report(spikes: pd.DataFrame, metric: str, threshold_pct: float) -> None:
    if spikes.empty:
        print(f"No {metric} spikes >= {threshold_pct:.0f}% detected.")
        return
    with session_scope() as session:
        apps = {
            a.id: a
            for a in session.execute(
                select(App).where(App.id.in_(spikes["app_id"].unique().tolist()))
            ).scalars()
        }
    print(f"\n{len(spikes)} {metric} spike(s) >= {threshold_pct:.0f}% over trailing MA:\n")
    header = f"{'date':<12} {'store':<5} {'app':<40} {'value':>15} {'baseline':>15} {'change':>9}"
    print(header)
    print("-" * len(header))
    for row in spikes.sort_values(["date", "pct_change"], ascending=[False, False]).itertuples():
        app = apps.get(row.app_id)
        name = (app.name or app.store_app_id)[:40] if app else str(row.app_id)
        store = app.store if app else "?"
        print(
            f"{row.date!s:<12} {store:<5} {name:<40} "
            f"{row.value:>15,.0f} {row.baseline:>15,.0f} {row.pct_change:>8.1f}%"
        )
    print()


def main() -> int:
    utf8_console()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--metric", choices=["revenue", "downloads", "installs"],
                        default="revenue")
    parser.add_argument("--threshold", type=float, default=settings.spike_threshold_pct,
                        help="spike threshold in percent (default %(default)s)")
    parser.add_argument("--window", type=int, default=settings.spike_ma_window_days,
                        help="moving-average window in days (default %(default)s)")
    parser.add_argument("--min-periods", type=int, default=settings.spike_min_periods,
                        help="min data points required in the window (default %(default)s; "
                             "clamped to --window)")
    parser.add_argument("--min-baseline", type=float, default=settings.spike_min_baseline,
                        help="ignore baselines below this value (default %(default)s)")
    parser.add_argument("--full-history", action="store_true",
                        help="scan all history instead of only each app's latest day")
    parser.add_argument("--dry-run", action="store_true", help="do not write alerts")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    df = load_metric_frame(args.metric)
    if df.empty:
        log.warning("No %s data found — run daily_snapshot.py first.", args.metric)
        return 0
    log.info("Loaded %d %s data points across %d apps",
             len(df), args.metric, df["app_id"].nunique())

    spikes = detect_spikes(
        df,
        threshold_pct=args.threshold,
        window_days=args.window,
        min_periods=args.min_periods,
        min_baseline=args.min_baseline,
        latest_only=not args.full_history,
    )
    print_report(spikes, args.metric, args.threshold)

    if not spikes.empty and not args.dry_run:
        written = persist_alerts(spikes, args.metric, args.window)
        log.info("Persisted %d new alert(s) (%d duplicates skipped)",
                 written, len(spikes) - written)
    return 0


if __name__ == "__main__":
    sys.exit(main())
