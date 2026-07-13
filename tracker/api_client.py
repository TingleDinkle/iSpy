"""Resilient AppstoreSpy API client.

Verified against the live API (July 2026):
  * Auth: ``API-KEY`` header. 403 = invalid key.
  * Base URL: https://api.appstorespy.com/v1
  * 429 = rate limited; transient 500s occur even on valid requests.
  * 202 = app queued for crawling (no data yet); 204 = no data for country.
  * 400/422 return ``{"detail": ...}`` — caller errors, never retried.
  * /estimates accepts comma-separated ids (batched here to save credits).
  * iOS review sort field is ``date`` (``-date`` = newest first). Play review
    date-sort 500s server-side, so Play reviews are fetched in default order.
  * Play reviews REQUIRE ``language`` from a fixed enum (e.g. ``en_US``).

Resilience strategy:
  * Client-side throttle (min interval between requests).
  * Retries with exponential backoff + full jitter on 429 / 5xx / network
    errors, honouring ``Retry-After`` when present.
  * Every request that reaches the API is written to the ``api_requests``
    ledger; a monthly credit budget guard refuses to exceed the plan.
"""

from __future__ import annotations

import datetime as dt
import logging
import random
import time
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Optional

import requests
from sqlalchemy import func, select

from .config import settings
from .db import SessionLocal
from .models import STORE_IOS, STORE_PLAY, ApiRequest

log = logging.getLogger(__name__)

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

# The reviews endpoints reject limit > 1000 with a 422 (documented API max).
REVIEWS_API_MAX_LIMIT = 1000


class AppstoreSpyError(Exception):
    """Base class for client errors."""


class AuthenticationError(AppstoreSpyError):
    """403 — API key rejected. Not retryable."""


class RequestFailed(AppstoreSpyError):
    """Non-retryable HTTP error (4xx other than 429)."""

    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"HTTP {status_code}: {detail}")


class RetriesExhausted(AppstoreSpyError):
    """Retryable errors persisted beyond max_retries."""


class CreditBudgetExhausted(AppstoreSpyError):
    """Monthly credit safety threshold reached; no further requests fired."""


def month_start_utc(now: Optional[dt.datetime] = None) -> dt.datetime:
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class CreditLedger:
    """Tracks month-to-date successful API calls against the plan budget.

    The count is loaded from the ``api_requests`` table and maintained in
    memory, re-synced from the database every ``RESYNC_EVERY`` requests so
    that concurrent processes writing to the same ledger stay visible (each
    process can overshoot the shared threshold by at most ~RESYNC_EVERY).
    The month anchor is re-checked on every ``check()``, so a run that
    crosses a month boundary picks up the fresh budget instead of the old
    month's count.
    """

    RESYNC_EVERY = 50

    def __init__(self) -> None:
        self.budget = settings.monthly_credit_budget
        self.threshold = int(self.budget * settings.credit_safety_fraction)
        self._month_start = month_start_utc()
        self._requests_since_resync = 0
        # Successful calls whose ledger row failed to persist. Added back on
        # every DB re-sync so the guard fails CLOSED even if the database
        # stops accepting writes mid-run.
        self._unpersisted = 0
        self._load_from_db()
        log.info(
            "Credit ledger: %s/%s used this month (safety threshold %s)",
            self.used_this_month, self.budget, self.threshold,
        )

    def _load_from_db(self) -> None:
        with SessionLocal() as session:
            db_count = session.execute(
                select(func.count())
                .select_from(ApiRequest)
                .where(
                    ApiRequest.called_at >= self._month_start,
                    ApiRequest.status_code.between(200, 299),
                )
            ).scalar_one()
        self.used_this_month = db_count + self._unpersisted
        self._requests_since_resync = 0

    def check(self) -> None:
        current_month = month_start_utc()
        if current_month != self._month_start:
            self._month_start = current_month
            self._unpersisted = 0  # unpersisted spend belonged to the old month
            self._load_from_db()
        elif self._requests_since_resync >= self.RESYNC_EVERY:
            self._load_from_db()  # pick up spend from concurrent processes
        if self.used_this_month >= self.threshold:
            raise CreditBudgetExhausted(
                f"{self.used_this_month} credits used this month; safety threshold "
                f"is {self.threshold} of {self.budget}. Raise MONTHLY_CREDIT_BUDGET "
                f"or wait for the monthly reset."
            )

    def record(self, endpoint: str, status_code: int, duration_ms: int) -> None:
        success = 200 <= status_code < 300
        if success:
            self.used_this_month += 1
        self._requests_since_resync += 1
        try:
            with SessionLocal() as session:
                session.add(
                    ApiRequest(endpoint=endpoint, status_code=status_code, duration_ms=duration_ms)
                )
                session.commit()
        except Exception:  # ledger writes must never take down a fetch run
            if success:
                self._unpersisted += 1
            log.exception("Failed to persist api_requests ledger row")


class AppstoreSpyClient:
    def __init__(self, ledger: Optional[CreditLedger] = None) -> None:
        self.ledger = ledger if ledger is not None else CreditLedger()
        self._session = requests.Session()
        self._session.headers.update(
            {"API-KEY": settings.appstorespy_api_key, "Accept": "application/json"}
        )
        self._min_interval = 1.0 / settings.requests_per_second
        self._last_request_monotonic = 0.0

    # ------------------------------------------------------------------ core

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_monotonic
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)

    @staticmethod
    def _backoff_delay(attempt: int, retry_after: Optional[str]) -> float:
        if retry_after:
            try:
                return min(float(retry_after), settings.backoff_max_seconds)
            except ValueError:
                pass  # not delta-seconds; RFC 9110 also allows an HTTP-date
            try:
                retry_at = parsedate_to_datetime(retry_after)
                delta = (retry_at - dt.datetime.now(dt.timezone.utc)).total_seconds()
                return max(0.0, min(delta, settings.backoff_max_seconds))
            except (TypeError, ValueError, OverflowError):
                pass  # unparseable Retry-After; fall through to jitter
        cap = min(settings.backoff_max_seconds, settings.backoff_base_seconds * (2 ** attempt))
        return random.uniform(0, cap)  # full jitter (AWS-style)

    def _request(
        self,
        path: str,
        params: Optional[dict[str, Any]] = None,
        *,
        method: str = "GET",
        json_body: Optional[dict[str, Any]] = None,
    ) -> Any:
        """Call ``path`` with retries. Returns parsed JSON, or None on 202/204."""
        url = f"{settings.api_base_url}{path}"
        last_error: str = ""

        for attempt in range(settings.max_retries + 1):
            self.ledger.check()
            self._throttle()
            started = time.monotonic()
            self._last_request_monotonic = started
            try:
                resp = self._session.request(
                    method, url, params=params, json=json_body,
                    timeout=settings.request_timeout_seconds,
                )
            except requests.RequestException as exc:
                # Covers ConnectionError/Timeout and also mid-body failures
                # (ChunkedEncodingError, ContentDecodingError, ...) — all
                # transient transport problems, all retryable.
                last_error = f"transport error: {exc.__class__.__name__}: {exc}"
                self.ledger.record(path, 0, int((time.monotonic() - started) * 1000))
                if attempt < settings.max_retries:
                    delay = self._backoff_delay(attempt, None)
                    log.warning("%s %s %s — retry %d/%d in %.1fs",
                                method, path, last_error, attempt + 1, settings.max_retries, delay)
                    time.sleep(delay)
                    continue
                break

            duration_ms = int((time.monotonic() - started) * 1000)
            self.ledger.record(path, resp.status_code, duration_ms)

            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    # e.g. an HTML maintenance page from a proxy/CDN with a
                    # 200 status — treat as a transient upstream mangle.
                    last_error = "invalid JSON in 200 response"
                    if attempt < settings.max_retries:
                        delay = self._backoff_delay(attempt, None)
                        log.warning("%s %s %s — retry %d/%d in %.1fs",
                                    method, path, last_error, attempt + 1, settings.max_retries, delay)
                        time.sleep(delay)
                        continue
                    break
            if resp.status_code in (202, 204):
                # 202: queued for crawling; 204: no data for this country.
                log.info("%s %s -> %d (no data available)", method, path, resp.status_code)
                return None
            if resp.status_code == 403:
                raise AuthenticationError("API key rejected (HTTP 403). Check APPSTORESPY_API_KEY.")
            if resp.status_code in RETRYABLE_STATUSES:
                last_error = f"HTTP {resp.status_code}"
                if attempt < settings.max_retries:
                    delay = self._backoff_delay(attempt, resp.headers.get("Retry-After"))
                    log.warning("%s %s %s — retry %d/%d in %.1fs",
                                method, path, last_error, attempt + 1, settings.max_retries, delay)
                    time.sleep(delay)
                    continue
                break

            # Remaining 4xx: caller error — surface the API's message, don't retry.
            try:
                body = resp.json()
            except ValueError:
                detail = resp.text
            else:
                detail = body.get("detail", resp.text) if isinstance(body, dict) else body
            raise RequestFailed(resp.status_code, detail)

        raise RetriesExhausted(f"{method} {path} failed after {settings.max_retries + 1} attempts ({last_error})")

    # ------------------------------------------------------------- endpoints

    def get_app(
        self,
        store: str,
        app_id: str,
        country: Optional[str] = None,
        fields: Optional[Iterable[str]] = None,
    ) -> Optional[dict[str, Any]]:
        """App details. Returns None if the app is queued for crawling (202)."""
        params: dict[str, Any] = {"country": country or settings.default_country}
        if fields:
            params["fields"] = ",".join(fields)
        return self._request(f"/{store}/apps/{app_id}", params)

    def get_estimates(
        self,
        store: str,
        app_ids: list[str],
        start: Optional[dt.date] = None,
        end: Optional[dt.date] = None,
    ) -> list[dict[str, Any]]:
        """Monthly revenue/downloads estimates, batched to save credits.

        Response rows: {"id": ..., "month": "YYYY-MM", "downloads": int, "revenue": int}
        """
        results: list[dict[str, Any]] = []
        params: dict[str, Any] = {}
        if start:
            params["start"] = start.isoformat()
        if end:
            params["end"] = end.isoformat()
        batch = settings.estimates_batch_size
        for i in range(0, len(app_ids), batch):
            chunk = app_ids[i : i + batch]
            data = self._request(f"/{store}/estimates", {**params, "id": ",".join(chunk)})
            if data:
                results.extend(data)
        return results

    def get_daily_installs(
        self,
        app_id: str,
        start: Optional[dt.date] = None,
        end: Optional[dt.date] = None,
    ) -> list[dict[str, Any]]:
        """Google Play daily installs. Rows: {"id", "date", "ipd", "installs"}."""
        params: dict[str, Any] = {}
        if start:
            params["start"] = start.isoformat()
        if end:
            params["end"] = end.isoformat()
        data = self._request(f"/play/apps/{app_id}/installs_daily", params)
        return data or []

    def get_reviews(
        self,
        store: str,
        app_id: str,
        country: Optional[str] = None,
        language: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Latest reviews.

        iOS supports ``sort=-date`` (newest first). Play's date sort 500s
        server-side, so Play reviews come back in the API's default
        (helpfulness-like) order — raise ``limit`` to widen coverage.
        """
        if limit is not None and limit <= 0:
            return []  # explicit zero means "fetch nothing" — don't burn a credit
        requested = settings.reviews_page_size if limit is None else limit
        if requested > REVIEWS_API_MAX_LIMIT:
            log.warning("Review limit %d exceeds the API max of %d — clamping",
                        requested, REVIEWS_API_MAX_LIMIT)
        params: dict[str, Any] = {"limit": min(requested, REVIEWS_API_MAX_LIMIT)}
        if store == STORE_IOS:
            params["country"] = country or settings.default_country
            params["sort"] = "-date"
        elif store == STORE_PLAY:
            params["language"] = language or settings.play_review_language
        else:
            raise ValueError(f"Unknown store: {store!r}")
        data = self._request(f"/{store}/apps/{app_id}/reviews", params)
        return data or []

    def get_rankings(
        self,
        store: str,
        country: str,
        category: str,
        date_start: dt.date,
        date_end: dt.date,
        rank_end: int = 200,
    ) -> list[dict[str, Any]]:
        """Top-chart positions, all collections (Free/Paid/Grossing/...), and
        both iPhone/iPad for iOS. Paginates through the ApiResponse envelope.

        Rows: {"date", "app", "country", "category", "collection", "rank",
        "platform" (iOS only)}. The live API returns occasional duplicate
        rows — callers should dedupe before bulk-writing.
        """
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._request(f"/{store}/rankings", {
                "date_start": date_start.isoformat(),
                "date_end": date_end.isoformat(),
                "country": country,
                "category": category,
                "rank_start": 1,
                "rank_end": rank_end,
                "limit": settings.rankings_page_size,
                "page": page,
            })
            if not data:
                break
            rows = data.get("data") or []
            results.extend(rows)
            total = data.get("total_count") or 0
            if len(rows) < settings.rankings_page_size or len(results) >= total:
                break
            page += 1
            if page > 50:  # safety valve: never loop forever on a bad total_count
                log.warning("Rankings pagination for %s/%s exceeded 50 pages — stopping",
                            store, category)
                break
        return results

    def query_apps(
        self,
        store: str,
        filter_body: dict[str, Any],
        fields: Optional[list[str]] = None,
        sort: Optional[str] = None,
        limit: int = 100,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """Structured app search (POST /{store}/apps/query).

        NOTE: ``fields`` here uses *filter-style* names (release_date,
        downloads_month, active_countries) — NOT the GET-endpoint names
        (released, downloads); the API 400s on the wrong dialect.
        """
        body: dict[str, Any] = {"filter": filter_body, "limit": limit, "page": page}
        if fields:
            body["fields"] = fields
        if sort:
            body["sort"] = sort
        data = self._request(f"/{store}/apps/query", method="POST", json_body=body)
        if not data:
            return []
        return data.get("data") or []

    def query_apps_all(
        self,
        store: str,
        filter_body: dict[str, Any],
        fields: Optional[list[str]] = None,
        sort: Optional[str] = None,
        page_size: int = 200,
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        """query_apps with pagination: fetches until a short page. Use for
        full-portfolio pulls — a single page silently truncates prolific
        publishers and later misreports old titles as new."""
        results: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            rows = self.query_apps(store, filter_body, fields=fields, sort=sort,
                                   limit=page_size, page=page)
            results.extend(rows)
            if len(rows) < page_size:
                return results
        log.warning("query_apps_all hit max_pages=%d for filter %s — portfolio "
                    "may be truncated", max_pages, filter_body)
        return results

    def get_summary(self, store: str, filter_body: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Aggregate market metrics for a filter (POST /{store}/apps/summary).
        Returns {"total", "ipd", "revenue", "downloads", "available", "removed"}.
        Filter ranges use {"from": x, "to": y} (both optional)."""
        return self._request(f"/{store}/apps/summary", method="POST",
                             json_body={"filter": filter_body})

    def get_developer(self, store: str, dev_id: str) -> Optional[dict[str, Any]]:
        """Developer/studio profile (name, totals, top_apps)."""
        return self._request(f"/{store}/developers/{dev_id}")

    def get_developer_estimates(
        self,
        store: str,
        dev_id: str,
        start: Optional[dt.date] = None,
        end: Optional[dt.date] = None,
    ) -> list[dict[str, Any]]:
        """Studio-level monthly revenue/downloads history.
        Rows: {"month": "YYYY-MM", "revenue": int, "downloads": int}."""
        params: dict[str, Any] = {}
        if start:
            params["start"] = start.isoformat()
        if end:
            params["end"] = end.isoformat()
        data = self._request(f"/{store}/developers/{dev_id}/estimates", params)
        return data or []

    def search_apps(
        self,
        store: str,
        q: str,
        country: Optional[str] = None,
        limit: int = 5,
        fields: Optional[Iterable[str]] = None,
        sort: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Free-text app search (GET /{store}/apps?q=...).

        App stores are full of clones with copycat names — sort by
        ``-downloads_month`` when resolving a famous title so the genuine
        app outranks the knockoffs.
        """
        params: dict[str, Any] = {
            "q": q,
            "country": country or settings.default_country,
            "limit": limit,
        }
        if fields:
            params["fields"] = ",".join(fields)
        if sort:
            params["sort"] = sort
        data = self._request(f"/{store}/apps", params)
        return data or []

    def get_liveops(
        self, app_id: str, country: Optional[str] = None, freshness: str = "7d"
    ) -> list[dict[str, Any]]:
        """Google Play LiveOps events for one app.

        WARNING: as of July 2026 this endpoint returns HTTP 500 on every
        probe — it is wired up for when AppstoreSpy fixes it. Expect
        RetriesExhausted until then.
        """
        params = {"app": app_id, "country": country or settings.default_country,
                  "freshness": freshness}
        data = self._request("/play/liveops", params)
        return data or []
