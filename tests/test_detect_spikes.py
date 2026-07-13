"""Unit tests for spike detection and Play-installs cleaning.

The tests exercise pure DataFrame logic — no database needed. Real-world
shapes from the live API (rounded monthly estimates, dirty installs feed with
cumulative dumps / negative resets / zero gaps) are reproduced verbatim.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from detect_spikes import _clean_installs, detect_spikes

D = dt.date  # brevity


def days(start: dt.date, n: int) -> list[dt.date]:
    return [start + dt.timedelta(days=i) for i in range(n)]


def frame(app_id: int, dates, values) -> pd.DataFrame:
    return pd.DataFrame({"app_id": app_id, "date": dates, "value": [float(v) for v in values]})


def run(df, threshold=50, window=7, min_periods=4, min_baseline=100, latest_only=True):
    return detect_spikes(df, threshold_pct=threshold, window_days=window,
                         min_periods=min_periods, min_baseline=min_baseline,
                         latest_only=latest_only)


START = D(2026, 7, 1)


# ------------------------------------------------------------ core behaviour


class TestSpikeMath:
    def test_60_percent_jump_alerts_with_exact_numbers(self):
        df = frame(1, days(START, 8), [1000] * 7 + [1600])
        out = run(df)
        assert len(out) == 1
        row = out.iloc[0]
        assert row["app_id"] == 1
        assert row["date"] == START + dt.timedelta(days=7)
        assert row["baseline"] == pytest.approx(1000)
        assert row["pct_change"] == pytest.approx(60)

    def test_exactly_50_percent_is_inclusive(self):
        df = frame(1, days(START, 8), [1000] * 7 + [1500])
        out = run(df)
        assert len(out) == 1
        assert out.iloc[0]["pct_change"] == pytest.approx(50)

    def test_just_below_threshold_does_not_alert(self):
        df = frame(1, days(START, 8), [1000] * 7 + [1499])
        assert run(df).empty

    def test_flat_series_never_alerts(self):
        df = frame(1, days(START, 30), [1000] * 30)
        assert run(df).empty

    def test_current_day_excluded_from_own_baseline(self):
        # If the spike day leaked into its own 7-day window, the baseline for
        # [1000x7, 8000] would be (6*1000+8000)/7 ≈ 2000, not 1000.
        df = frame(1, days(START, 8), [1000] * 7 + [8000])
        out = run(df)
        assert out.iloc[0]["baseline"] == pytest.approx(1000)

    def test_drop_below_baseline_is_not_a_spike(self):
        df = frame(1, days(START, 8), [1000] * 7 + [200])
        assert run(df).empty

    def test_custom_threshold_and_window(self):
        # +70% over a 14-day baseline
        df = frame(1, days(START, 15), [1000] * 14 + [1700])
        assert len(run(df, threshold=70, window=14)) == 1
        assert run(df, threshold=71, window=14).empty


class TestMissingDays:
    def test_sparse_history_below_min_periods_never_alerts(self):
        # Only 3 observations in the prior 7 calendar days -> no trusted baseline.
        dates = [START, START + dt.timedelta(days=2), START + dt.timedelta(days=4),
                 START + dt.timedelta(days=6)]
        df = frame(1, dates, [1000, 1000, 1000, 9000])
        assert run(df, min_periods=4).empty

    def test_exactly_min_periods_observations_is_enough(self):
        # 4 observations inside the prior 7 days -> baseline exists.
        dates = [START + dt.timedelta(days=i) for i in (0, 2, 4, 6)] + [START + dt.timedelta(days=7)]
        df = frame(1, dates, [1000, 1000, 1000, 1000, 1600])
        out = run(df, min_periods=4)
        assert len(out) == 1
        assert out.iloc[0]["baseline"] == pytest.approx(1000)

    def test_long_gap_invalidates_baseline(self):
        # 17-day silence: the prior 7-day window of the resume day is empty.
        dates = days(START, 3) + [START + dt.timedelta(days=19)]
        df = frame(1, dates, [1000, 1000, 1000, 1600])
        assert run(df).empty

    def test_series_resuming_after_gap_realerts_once_rebaselined(self):
        # After 7 fresh post-gap days the detector must work again.
        dates = days(START, 3) + days(START + dt.timedelta(days=20), 8)
        df = frame(1, dates, [500, 500, 500] + [1000] * 7 + [1600])
        out = run(df)
        assert len(out) == 1
        assert out.iloc[0]["baseline"] == pytest.approx(1000)  # old pre-gap data ignored

    def test_gap_days_are_not_compressed_away(self):
        # 4 observations spread over 12 calendar days: a row-based window of 7
        # would see all of them; a calendar window sees at most 2 -> no alert.
        dates = [START + dt.timedelta(days=i) for i in (0, 4, 8, 12)]
        df = frame(1, dates, [1000, 1000, 1000, 1600])
        assert run(df).empty

    def test_missing_latest_days_do_not_block_spike_on_last_data_day(self):
        # The spike is on the app's most recent *data* day; later calendar
        # days simply don't exist yet.
        df = frame(1, days(START, 8), [1000] * 7 + [1600])
        out = run(df, latest_only=True)
        assert len(out) == 1


class TestAlertScoping:
    def test_latest_only_ignores_historical_spikes(self):
        values = [1000] * 7 + [1600] + [1000] * 2  # spike then back to normal
        df = frame(1, days(START, 10), values)
        assert run(df, latest_only=True).empty

    def test_full_history_finds_historical_spikes(self):
        values = [1000] * 7 + [1600] + [1000] * 2
        df = frame(1, days(START, 10), values)
        out = run(df, latest_only=False)
        assert len(out) == 1
        assert out.iloc[0]["date"] == START + dt.timedelta(days=7)

    def test_min_baseline_floor_suppresses_tiny_apps(self):
        df = frame(1, days(START, 8), [10] * 7 + [16])
        assert run(df, min_baseline=1000).empty
        assert len(run(df, min_baseline=5)) == 1

    def test_zero_baseline_never_alerts_even_with_floor_zero(self):
        df = frame(1, days(START, 8), [0] * 7 + [500])
        assert run(df, min_baseline=0).empty

    def test_multiple_apps_do_not_cross_contaminate(self):
        flat = frame(1, days(START, 8), [1000] * 8)
        spike = frame(2, days(START, 8), [1000] * 7 + [1600])
        other_range = frame(3, days(D(2026, 5, 1), 8), [50] * 8)  # different date range
        out = run(pd.concat([flat, spike, other_range], ignore_index=True))
        assert list(out["app_id"]) == [2]

    def test_empty_frame_returns_empty_with_columns(self):
        out = run(pd.DataFrame({"app_id": [], "date": [], "value": []}))
        assert out.empty
        assert list(out.columns) == ["app_id", "date", "value", "baseline", "pct_change"]

    def test_single_row_returns_empty(self):
        assert run(frame(1, [START], [1000])).empty


class TestInputRobustness:
    """Data shapes production can actually deliver (or a CLI user can cause)."""

    def test_window_smaller_than_min_periods_clamps_instead_of_crashing(self):
        # `--window 3` with the default min_periods=4 used to raise
        # "min_periods 4 must be <= window 3" from pandas mid-run.
        df = frame(1, days(START, 8), [1000] * 7 + [1600])
        out = run(df, window=3, min_periods=4)
        assert len(out) == 1
        assert out.iloc[0]["baseline"] == pytest.approx(1000)

    def test_unsorted_input_handled(self):
        df = frame(1, days(START, 8), [1000] * 7 + [1600]).sample(frac=1, random_state=42)
        out = run(df)
        assert len(out) == 1
        assert out.iloc[0]["baseline"] == pytest.approx(1000)

    def test_int64_dtype_from_read_sql_handled(self):
        # pd.read_sql yields int64 for BigInteger columns with no NULLs.
        df = pd.DataFrame({"app_id": 1, "date": days(START, 8),
                           "value": [1000] * 7 + [1600]})
        assert df["value"].dtype == np.int64
        assert len(run(df)) == 1

    def test_decimal_values_from_numeric_columns_handled(self):
        # Postgres Numeric columns arrive as object-dtype Decimals.
        df = pd.DataFrame({"app_id": 1, "date": days(START, 8),
                           "value": [Decimal("1000")] * 7 + [Decimal("1600")]})
        out = run(df)
        assert len(out) == 1
        assert out.iloc[0]["pct_change"] == pytest.approx(60)

    def test_none_values_in_value_column_handled(self):
        # Nullable columns yield None/NaN mixed in; they must simply drop out
        # of the baseline count, not poison it.
        df = pd.DataFrame({"app_id": 1, "date": days(START, 8),
                           "value": [1000, None, 1000, 1000, None, 1000, 1000, 1600]})
        out = run(df)
        assert len(out) == 1
        assert out.iloc[0]["baseline"] == pytest.approx(1000)

    def test_duplicate_dates_keep_last_instead_of_crashing(self):
        df = frame(1, days(START, 8), [1000] * 7 + [1600])
        stale = frame(1, [START + dt.timedelta(days=3)], [999])  # duplicate day
        out = run(pd.concat([stale, df], ignore_index=True))  # later row wins
        assert len(out) == 1
        assert out.iloc[0]["baseline"] == pytest.approx(1000)

    def test_time_of_day_timestamps_do_not_break_daily_grid(self):
        # If the date column ever becomes a DateTime, drifting wall-clock
        # times must not silently misalign the daily re-index.
        dates = [dt.datetime(2026, 7, 1, 14, 30) + dt.timedelta(days=i, minutes=i * 7)
                 for i in range(8)]
        df = pd.DataFrame({"app_id": 1, "date": dates,
                           "value": [1000.0] * 7 + [1600.0]})
        out = run(df)
        assert len(out) == 1
        assert out.iloc[0]["pct_change"] == pytest.approx(60)

    def test_all_nan_group_after_cleaning_returns_empty(self):
        df = installs_frame([0] * 10, [500] * 10)
        assert run(_clean_installs(df)).empty


# ------------------------------------------------------- installs cleaning


def installs_frame(values, cumulative, start=START, app_id=7):
    n = len(values)
    return pd.DataFrame({
        "app_id": app_id,
        "date": days(start, n),
        "value": values,
        "installs_cumulative": cumulative,
    })


class TestCleanInstalls:
    def test_live_candy_crush_artifacts_are_masked(self):
        """The exact dirty rows observed on the live API for Candy Crush."""
        df = installs_frame(
            values=[0, 0, 0, 2271389663, 2882594, 726917, 710136, -2275709310, 0, 0, 0],
            cumulative=[0, 0, 0, 2271389663, 2274272257, 2274999174, 2275709310, 0, 0, 0, 0],
        )
        kept = _clean_installs(df).dropna(subset=["value"])["value"].tolist()
        assert sorted(kept) == [710136.0, 726917.0, 2882594.0]

    def test_zeros_and_negatives_masked(self):
        df = installs_frame([0, -5, 100, 200], [1000, 995, 1095, 1295])
        kept = _clean_installs(df).dropna(subset=["value"])["value"].tolist()
        assert kept == [100.0, 200.0]

    def test_cumulative_dump_day_masked(self):
        # ipd == lifetime counter -> backfill dump, not a real day.
        df = installs_frame([1000] * 6 + [5_000_000], [10_000 + i for i in range(6)] + [5_000_000])
        cleaned = _clean_installs(df)
        assert np.isnan(cleaned["value"].iloc[-1])
        assert cleaned["value"].iloc[:6].notna().all()

    def test_extreme_outlier_vs_rolling_median_masked(self):
        # 5000x the median with a cumulative that does NOT equal ipd (so the
        # dump-equality mask can't catch it) must still be masked.
        df = installs_frame([1000] * 13 + [5_000_000], [50_000 + i * 1000 for i in range(13)] + [999_999])
        cleaned = _clean_installs(df)
        assert np.isnan(cleaned["value"].iloc[-1])

    def test_genuine_viral_spike_survives_cleaning(self):
        # 25x day-over-day is a big featured-by-the-store day, not an artifact.
        # The artifact filter must not eat the exact signal we want to detect.
        df = installs_frame([1000] * 13 + [25_000], [50_000 + i * 1000 for i in range(13)] + [76_000])
        cleaned = _clean_installs(df)
        assert cleaned["value"].iloc[-1] == pytest.approx(25_000)

    def test_short_history_kept_when_median_unavailable(self):
        # Fewer than 5 observations: rolling median is NaN -> no outlier
        # verdict possible -> values must be kept, not dropped.
        df = installs_frame([100, 200, 150], [1000, 1200, 1350])
        cleaned = _clean_installs(df)
        assert cleaned["value"].notna().all()

    def test_none_cumulative_handled(self):
        df = installs_frame([100, 200, 300], [None, None, None])
        cleaned = _clean_installs(df)
        assert cleaned["value"].notna().all()

    def test_output_drops_cumulative_column(self):
        df = installs_frame([100], [1000])
        assert "installs_cumulative" not in _clean_installs(df).columns

    def test_dump_in_first_rows_still_masked(self):
        """A dump inside an app's first 4 rows has no rolling-median verdict;
        the global-median fallback must catch it, or it poisons every 7-day
        baseline that includes it and suppresses genuine alerts."""
        values = [1000, 1000, 1000, 50_000_000] + [1000] * 6 + [1600]
        cumulative = [10_000 + i * 1000 for i in range(11)]  # never equals ipd
        cleaned = _clean_installs(installs_frame(values, cumulative))
        assert np.isnan(cleaned["value"].iloc[3])
        out = run(cleaned)
        assert len(out) == 1
        assert out.iloc[0]["baseline"] == pytest.approx(1000)
        assert out.iloc[0]["pct_change"] == pytest.approx(60)


class TestCleanedInstallsFeedDetection:
    def test_end_to_end_dirty_feed_to_alert(self):
        """A dump artifact mid-series must not poison the baseline, and the
        genuine +60% on the latest day must still alert."""
        values = [1000, 1000, 1000, 1000, 1000, 9_999_999, 1000, 1000, 1000, 1000, 1000, 1600]
        cumulative = [10_000 + i * 1000 for i in range(5)] + [9_999_999] + \
                     [20_000 + i * 1000 for i in range(6)]
        df = installs_frame(values, cumulative)
        cleaned = _clean_installs(df)
        out = run(cleaned)
        assert len(out) == 1
        assert out.iloc[0]["baseline"] == pytest.approx(1000)
        assert out.iloc[0]["pct_change"] == pytest.approx(60)

    def test_artifact_day_itself_never_alerts(self):
        values = [1000] * 11 + [9_999_999]
        cumulative = [10_000 + i * 1000 for i in range(11)] + [9_999_999]
        df = installs_frame(values, cumulative)
        assert run(_clean_installs(df)).empty

    def test_spike_on_last_data_day_with_masked_trailing_artifact(self):
        # Genuine +60% on day 12, dump artifact on day 13 that cleaning masks
        # to NaN: the alert must anchor to the last *data* day, not the last
        # calendar row.
        values = [1000] * 11 + [1600, 9_999_999]
        cumulative = [10_000 + i * 1000 for i in range(12)] + [9_999_999]
        out = run(_clean_installs(installs_frame(values, cumulative)))
        assert len(out) == 1
        assert out.iloc[0]["date"] == START + dt.timedelta(days=11)
        assert out.iloc[0]["pct_change"] == pytest.approx(60)
