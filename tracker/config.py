"""Application settings, loaded from environment variables / .env."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Anchor .env to the project root, not the process CWD — schedulers (cron,
# Windows Task Scheduler) start jobs from an arbitrary working directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # --- Credentials / connections ---
    appstorespy_api_key: str
    database_url: str = "postgresql+psycopg://tracker:tracker@localhost:5432/appstore_tracker"
    api_base_url: str = "https://api.appstorespy.com/v1"

    # --- Credit budget ---
    # AppstoreSpy plan allowance per calendar month. The client refuses to fire
    # requests once usage reaches monthly_credit_budget * credit_safety_fraction,
    # so a runaway loop can never burn the whole allowance.
    monthly_credit_budget: int = 100_000
    credit_safety_fraction: float = 0.95

    # --- HTTP behaviour ---
    requests_per_second: float = 2.0
    max_retries: int = 5
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 60.0
    request_timeout_seconds: float = 30.0

    # --- Notifications (optional; paste webhook URLs into .env, never commit) ---
    discord_webhook_url: str | None = None
    slack_webhook_url: str | None = None

    # --- Data collection defaults ---
    default_country: str = "US"
    # Play reviews require a language from the API's enum:
    # en_US, ar, en_GB, fr_FR, es_419, de_DE, pt_BR, it_IT, ja_JP, ko_KR, tr_TR, ru_RU, vi
    play_review_language: str = "en_US"
    reviews_page_size: int = 200  # API max is 1000
    estimates_lookback_days: int = 130   # ~4 months; estimates get revised retroactively
    installs_lookback_days: int = 180    # first-run backfill window for Play daily installs
    installs_refetch_days: int = 7       # re-pull recent days to pick up corrections
    estimates_batch_size: int = 25       # app ids per /estimates call (comma-separated)
    watch_refresh_days: int = 7          # 'watch'-tier apps refresh at most this often
    rankings_lookback_days: int = 3      # chart feed lags ~2 days upstream; cover 2+ days
    rankings_page_size: int = 1000       # API max for /rankings

    # --- Event detection ---
    rank_track_depth: int = 200          # default rank_end for new ranking watches
    rank_entry_top: int = 50             # 'chart_entry' fires for apps entering this range
    rank_jump_min: int = 20              # min positions gained for a 'rank_jump' event
    soft_launch_max_countries: int = 15  # <= this many storefronts (no US/GB) = soft launch

    # --- Review mining ---
    rating_drop_stars: float = 0.5       # 7d avg star drop that triggers an alert
    rating_min_reviews: int = 5          # min reviews in the window to trust the average
    topic_surge_min: int = 5             # min mentions this window to consider a surge
    topic_surge_ratio: float = 3.0       # vs prior window

    # --- Spike detection defaults ---
    # Play-installs artifact filter: a day exceeding this multiple of the
    # rolling median is masked as a data artifact. Live artifacts (cumulative
    # dumps) run 1000x+; genuine viral days rarely exceed ~50x — keep this
    # well above real spikes so the filter never eats the signal.
    installs_outlier_multiplier: float = 100.0
    spike_threshold_pct: float = 50.0
    spike_ma_window_days: int = 7
    spike_min_periods: int = 4        # min prior data points before a baseline is trusted
    spike_min_baseline: float = 1000  # ignore spikes on tiny baselines (noise floor)


settings = Settings()
