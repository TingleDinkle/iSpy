"""SQLAlchemy ORM models.

Schema overview
---------------
apps               registry of tracked apps (store + store_app_id unique);
                   tier 'primary' = full daily pull, 'watch' = weekly light pull
app_snapshots      one row per app per day: metadata + AppstoreSpy's rolling
                   monthly revenue/downloads estimate as observed that day
monthly_estimates  AppstoreSpy monthly revenue/downloads history (/estimates)
daily_installs     Google Play only: true daily install counts (/installs_daily)
reviews            append-only review store, deduped per (app, store_review_id);
                   topics filled in by analyze_reviews.py
api_requests       ledger of every HTTP call — powers the credit budget guard
alerts             detected metric spikes/drops, deduped per (app, metric, date)
rankings           daily top-chart positions for watched category charts
                   (store_app_id is NOT an FK — charts contain untracked apps)
ranking_watches    which (store, country, category) charts to pull daily
developers         tracked studios; their app portfolios are re-queried daily
developer_apps     every app ever seen from a tracked studio (new-game radar)
app_events         qualitative intelligence feed: version updates, creative
                   changes, UA flips, soft launches, chart moves, topic surges
market_segments    saved market filters (e.g. "Play puzzle >100k dl/mo")
market_snapshots   weekly aggregate metrics per segment (/apps/summary)
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

STORE_IOS = "ios"
STORE_PLAY = "play"

TIER_PRIMARY = "primary"  # full daily pull: details, estimates, installs, reviews
TIER_WATCH = "watch"      # weekly light pull: details + estimates only


class Base(DeclarativeBase):
    pass


class App(Base):
    """A tracked app. store_app_id is the Apple numeric id or the Play bundle id."""

    __tablename__ = "apps"
    __table_args__ = (
        UniqueConstraint("store", "store_app_id", name="uq_apps_store_app"),
        CheckConstraint("store IN ('ios', 'play')", name="ck_apps_store"),
        CheckConstraint("tier IN ('primary', 'watch')", name="ck_apps_tier"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store: Mapped[str] = mapped_column(String(4), nullable=False)
    store_app_id: Mapped[str] = mapped_column(String(255), nullable=False)
    bundle_id: Mapped[Optional[str]] = mapped_column(String(255))
    name: Mapped[Optional[str]] = mapped_column(Text)
    developer_name: Mapped[Optional[str]] = mapped_column(Text)
    category: Mapped[Optional[str]] = mapped_column(Text)  # comma-joined if multiple
    country: Mapped[str] = mapped_column(String(2), nullable=False, default="US")
    tier: Mapped[str] = mapped_column(String(10), nullable=False, default=TIER_PRIMARY)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    snapshots: Mapped[list["AppSnapshot"]] = relationship(back_populates="app")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<App {self.store}:{self.store_app_id} {self.name!r}>"


class AppSnapshot(Base):
    """Daily observation of an app's metadata and rolling monthly estimates.

    AppstoreSpy exposes revenue/downloads as *rolling monthly estimates* on the
    app-details endpoint; sampling them daily yields the time series the spike
    detector runs on. Values are heavily rounded upstream (e.g. 50_000_000).
    """

    __tablename__ = "app_snapshots"
    __table_args__ = (
        UniqueConstraint("app_id", "snapshot_date", name="uq_snapshots_app_date"),
        Index("ix_snapshots_app_date", "app_id", "snapshot_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), nullable=False)
    snapshot_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(Text)
    version: Mapped[Optional[str]] = mapped_column(String(64))
    rating_value: Mapped[Optional[float]] = mapped_column(Numeric(3, 2))
    rating_count: Mapped[Optional[int]] = mapped_column(BigInteger)
    revenue_monthly_est: Mapped[Optional[int]] = mapped_column(BigInteger)   # USD
    downloads_monthly_est: Mapped[Optional[int]] = mapped_column(BigInteger)
    raw: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    fetched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    app: Mapped[App] = relationship(back_populates="snapshots")


class MonthlyEstimate(Base):
    """AppstoreSpy monthly revenue/downloads history (GET /{store}/estimates)."""

    __tablename__ = "monthly_estimates"
    __table_args__ = (
        UniqueConstraint("app_id", "month", name="uq_estimates_app_month"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), nullable=False)
    month: Mapped[dt.date] = mapped_column(Date, nullable=False)  # first day of month
    revenue: Mapped[Optional[int]] = mapped_column(BigInteger)    # USD
    downloads: Mapped[Optional[int]] = mapped_column(BigInteger)
    fetched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DailyInstalls(Base):
    """Google Play daily installs (GET /play/apps/{id}/installs_daily).

    Raw feed is noisy: zero-filled gaps, cumulative-counter dumps, and negative
    resets all occur. Stored verbatim; cleaning happens at analysis time.
    """

    __tablename__ = "daily_installs"
    __table_args__ = (
        UniqueConstraint("app_id", "date", name="uq_installs_app_date"),
        Index("ix_installs_app_date", "app_id", "date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), nullable=False)
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    ipd: Mapped[Optional[int]] = mapped_column(BigInteger)                  # installs per day
    installs_cumulative: Mapped[Optional[int]] = mapped_column(BigInteger)  # lifetime counter
    fetched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("app_id", "store_review_id", name="uq_reviews_app_review"),
        Index("ix_reviews_app_created", "app_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), nullable=False)
    store_review_id: Mapped[str] = mapped_column(String(255), nullable=False)
    stars: Mapped[Optional[int]] = mapped_column(SmallInteger)
    title: Mapped[Optional[str]] = mapped_column(Text)          # iOS only
    comment: Mapped[Optional[str]] = mapped_column(Text)
    author_name: Mapped[Optional[str]] = mapped_column(Text)
    country: Mapped[Optional[str]] = mapped_column(String(8))   # iOS only
    lang: Mapped[Optional[str]] = mapped_column(String(8))      # Play only
    app_version: Mapped[Optional[str]] = mapped_column(String(64))
    likes: Mapped[Optional[int]] = mapped_column(Integer)       # Play only
    created_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    topics: Mapped[Optional[list[str]]] = mapped_column(JSONB)  # filled by analyze_reviews.py
    raw: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    fetched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ApiRequest(Base):
    """One row per HTTP request that reached the API. Successful (2xx) rows
    count against the monthly credit budget."""

    __tablename__ = "api_requests"
    __table_args__ = (Index("ix_api_requests_called_at", "called_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    called_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)  # path, no query string
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)  # 0 = network error
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)


class Alert(Base):
    """A detected spike: metric value vs its trailing moving-average baseline."""

    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint("app_id", "metric", "alert_date", name="uq_alerts_app_metric_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), nullable=False)
    metric: Mapped[str] = mapped_column(String(32), nullable=False)  # revenue|downloads|installs
    alert_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    value: Mapped[float] = mapped_column(Numeric(20, 2), nullable=False)
    baseline: Mapped[float] = mapped_column(Numeric(20, 2), nullable=False)
    pct_change: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    window_days: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    notified_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Ranking(Base):
    """One chart position observation. Charts contain mostly untracked apps,
    so store_app_id is a plain string, not a foreign key."""

    __tablename__ = "rankings"
    __table_args__ = (
        UniqueConstraint("store", "date", "country", "category", "collection",
                         "platform", "store_app_id", name="uq_rankings_slot"),
        Index("ix_rankings_chart_date", "store", "country", "category",
              "collection", "platform", "date"),
        Index("ix_rankings_app", "store", "store_app_id", "date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    store: Mapped[str] = mapped_column(String(4), nullable=False)
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    store_app_id: Mapped[str] = mapped_column(String(255), nullable=False)
    platform: Mapped[str] = mapped_column(String(12), nullable=False, default="all")  # iPhone|iPad|all
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    category: Mapped[str] = mapped_column(String(48), nullable=False)
    collection: Mapped[str] = mapped_column(String(32), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RankingWatch(Base):
    """A category chart to pull daily (all collections, both iOS platforms)."""

    __tablename__ = "ranking_watches"
    __table_args__ = (
        UniqueConstraint("store", "country", "category", name="uq_watches_chart"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store: Mapped[str] = mapped_column(String(4), nullable=False)
    country: Mapped[str] = mapped_column(String(2), nullable=False, default="US")
    category: Mapped[str] = mapped_column(String(48), nullable=False)
    rank_depth: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Developer(Base):
    """A tracked studio. Its full app portfolio is re-queried daily; apps not
    seen before become 'new_developer_app' events (the new-game radar)."""

    __tablename__ = "developers"
    __table_args__ = (
        UniqueConstraint("store", "store_dev_id", name="uq_developers_store_dev"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store: Mapped[str] = mapped_column(String(4), nullable=False)
    store_dev_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[Optional[str]] = mapped_column(Text)
    auto_track: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DeveloperApp(Base):
    """Every app ever observed in a tracked studio's portfolio."""

    __tablename__ = "developer_apps"
    __table_args__ = (
        UniqueConstraint("developer_id", "store_app_id", name="uq_dev_apps"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    developer_id: Mapped[int] = mapped_column(
        ForeignKey("developers.id", ondelete="CASCADE"), nullable=False
    )
    store_app_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[Optional[str]] = mapped_column(Text)
    release_date: Mapped[Optional[dt.date]] = mapped_column(Date)
    first_seen: Mapped[dt.date] = mapped_column(Date, nullable=False)
    raw: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)


class AppEvent(Base):
    """Qualitative intelligence feed. dedupe_key makes re-runs idempotent."""

    __tablename__ = "app_events"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_events_dedupe"),
        Index("ix_events_date", "event_date"),
        Index("ix_events_app", "app_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    app_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("apps.id", ondelete="SET NULL")
    )
    store: Mapped[Optional[str]] = mapped_column(String(4))
    store_app_id: Mapped[Optional[str]] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    event_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    dedupe_key: Mapped[str] = mapped_column(String(512), nullable=False)
    notified_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MarketSegment(Base):
    """A saved market filter, e.g. Play puzzle games above 100k downloads/mo."""

    __tablename__ = "market_segments"
    __table_args__ = (UniqueConstraint("name", name="uq_segments_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    store: Mapped[str] = mapped_column(String(4), nullable=False)
    filter: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MarketSnapshot(Base):
    """Aggregate segment metrics from POST /{store}/apps/summary."""

    __tablename__ = "market_snapshots"
    __table_args__ = (
        UniqueConstraint("segment_id", "date", name="uq_market_segment_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    segment_id: Mapped[int] = mapped_column(
        ForeignKey("market_segments.id", ondelete="CASCADE"), nullable=False
    )
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    total: Mapped[Optional[int]] = mapped_column(BigInteger)
    available: Mapped[Optional[int]] = mapped_column(BigInteger)
    removed: Mapped[Optional[int]] = mapped_column(BigInteger)
    ipd: Mapped[Optional[int]] = mapped_column(BigInteger)
    revenue: Mapped[Optional[int]] = mapped_column(BigInteger)
    downloads: Mapped[Optional[int]] = mapped_column(BigInteger)
    fetched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
