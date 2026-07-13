"""Unit tests for the resilient API client: throttling, exponential backoff,
retry classification, credit ledger, batching, and pagination limits.

No test touches the network or a database — HTTP, the clock, and the ledger
are all faked.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
import requests

from tracker import api_client as mod
from tracker.api_client import (
    AppstoreSpyClient,
    AuthenticationError,
    CreditBudgetExhausted,
    CreditLedger,
    RequestFailed,
    RetriesExhausted,
    month_start_utc,
)
from tracker.config import settings

# --------------------------------------------------------------------- fakes

INVALID_JSON = object()  # sentinel: response body that is not JSON


class FakeResponse:
    def __init__(self, status_code, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._json is INVALID_JSON:
            raise ValueError("body is not JSON")
        return self._json


class FakeSession:
    """Plays back a script of FakeResponses / exceptions, records every call."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def request(self, method, url, params=None, json=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": dict(params or {}),
                           "json": json, "timeout": timeout})
        assert self.script, "client made more HTTP calls than the test scripted"
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, params=None, timeout=None):
        return self.request("GET", url, params=params, timeout=timeout)


class FakeTime:
    """Replaces the `time` module inside tracker.api_client: monotonic() is a
    settable clock, sleep() records durations and advances the clock."""

    def __init__(self, start=1000.0):
        self.now = start
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeLedger:
    def __init__(self, exhausted=False):
        self.exhausted = exhausted
        self.records = []
        self.checks = 0
        # attrs the snapshot script reads for its summary line
        self.used_this_month = 0
        self.budget = 100_000

    def check(self):
        self.checks += 1
        if self.exhausted:
            raise CreditBudgetExhausted("budget exhausted (test)")

    def record(self, endpoint, status_code, duration_ms):
        self.records.append((endpoint, status_code))
        if 200 <= status_code < 300:
            self.used_this_month += 1


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def fake_time(monkeypatch):
    ft = FakeTime()
    monkeypatch.setattr(mod, "time", ft)
    return ft


@pytest.fixture
def no_jitter(monkeypatch):
    """Make full-jitter deterministic: random.uniform(a, b) -> b (worst case)."""
    monkeypatch.setattr(mod, "random", SimpleNamespace(uniform=lambda a, b: b))


def make_client(script, ledger=None, throttled=False):
    client = AppstoreSpyClient(ledger=ledger or FakeLedger())
    client._session = FakeSession(script)
    if not throttled:
        client._min_interval = 0.0  # keep backoff assertions free of throttle sleeps
    return client


# ------------------------------------------------------------- rate limiter


class TestRateLimiter:
    def test_second_request_waits_min_interval(self, fake_time, monkeypatch):
        monkeypatch.setattr(settings, "requests_per_second", 4.0)  # 0.25s interval
        client = make_client([FakeResponse(200, {}), FakeResponse(200, {})], throttled=True)
        client._request("/a")
        client._request("/b")
        assert fake_time.sleeps == [pytest.approx(0.25)]

    def test_no_wait_when_enough_time_elapsed(self, fake_time, monkeypatch):
        monkeypatch.setattr(settings, "requests_per_second", 4.0)
        client = make_client([FakeResponse(200, {}), FakeResponse(200, {})], throttled=True)
        client._request("/a")
        fake_time.now += 1.0  # more than the 0.25s interval
        client._request("/b")
        assert fake_time.sleeps == []

    def test_partial_elapsed_waits_remainder(self, fake_time, monkeypatch):
        monkeypatch.setattr(settings, "requests_per_second", 2.0)  # 0.5s interval
        client = make_client([FakeResponse(200, {}), FakeResponse(200, {})], throttled=True)
        client._request("/a")
        fake_time.now += 0.2
        client._request("/b")
        assert fake_time.sleeps == [pytest.approx(0.3)]

    def test_interval_derived_from_settings(self, monkeypatch):
        monkeypatch.setattr(settings, "requests_per_second", 10.0)
        client = AppstoreSpyClient(ledger=FakeLedger())
        assert client._min_interval == pytest.approx(0.1)


# ------------------------------------------------------- backoff and retries


class TestBackoffAndRetries:
    def test_exponential_backoff_with_jitter_upper_bound(self, fake_time, no_jitter, monkeypatch):
        monkeypatch.setattr(settings, "max_retries", 3)
        monkeypatch.setattr(settings, "backoff_base_seconds", 1.0)
        monkeypatch.setattr(settings, "backoff_max_seconds", 60.0)
        client = make_client([FakeResponse(500)] * 4)
        with pytest.raises(RetriesExhausted):
            client._request("/x")
        assert fake_time.sleeps == [1.0, 2.0, 4.0]  # base * 2^attempt, no 4th sleep
        assert len(client._session.calls) == 4  # initial + 3 retries

    def test_backoff_capped_at_max(self, fake_time, no_jitter, monkeypatch):
        monkeypatch.setattr(settings, "max_retries", 2)
        monkeypatch.setattr(settings, "backoff_base_seconds", 32.0)
        monkeypatch.setattr(settings, "backoff_max_seconds", 60.0)
        client = make_client([FakeResponse(503)] * 3)
        with pytest.raises(RetriesExhausted):
            client._request("/x")
        assert fake_time.sleeps == [32.0, 60.0]  # 64 capped to 60

    def test_retry_after_header_honored(self, fake_time):
        client = make_client([
            FakeResponse(429, headers={"Retry-After": "7"}),
            FakeResponse(200, {"ok": 1}),
        ])
        assert client._request("/x") == {"ok": 1}
        assert fake_time.sleeps == [7.0]

    def test_retry_after_capped_at_backoff_max(self, fake_time, monkeypatch):
        monkeypatch.setattr(settings, "backoff_max_seconds", 60.0)
        client = make_client([
            FakeResponse(429, headers={"Retry-After": "600"}),
            FakeResponse(200, {}),
        ])
        client._request("/x")
        assert fake_time.sleeps == [60.0]

    def test_http_date_retry_after_honored_and_clamped(self, fake_time, monkeypatch):
        """RFC 9110 allows Retry-After as an HTTP-date; it must be waited out
        (clamped to backoff_max), not silently replaced by a shorter jitter."""
        monkeypatch.setattr(settings, "backoff_max_seconds", 60.0)
        client = make_client([
            FakeResponse(429, headers={"Retry-After": "Fri, 01 Jan 2100 00:00:00 GMT"}),
            FakeResponse(200, {}),
        ])
        client._request("/x")
        assert fake_time.sleeps == [60.0]  # far-future date clamped to backoff_max

    def test_past_http_date_retry_after_retries_immediately(self, fake_time):
        client = make_client([
            FakeResponse(429, headers={"Retry-After": "Mon, 01 Jan 2018 00:00:00 GMT"}),
            FakeResponse(200, {}),
        ])
        client._request("/x")
        assert fake_time.sleeps == [0.0]

    def test_garbage_retry_after_falls_back_to_jitter(self, fake_time, no_jitter, monkeypatch):
        monkeypatch.setattr(settings, "backoff_base_seconds", 1.0)
        client = make_client([
            FakeResponse(429, headers={"Retry-After": "soon"}),
            FakeResponse(200, {}),
        ])
        client._request("/x")
        assert fake_time.sleeps == [1.0]

    def test_throttle_still_enforced_during_zero_delay_retry(self, fake_time, monkeypatch):
        """A server saying 'retry now' (Retry-After: 0) must not defeat the
        client-side rate limit: attempt starts stay min_interval apart."""
        monkeypatch.setattr(settings, "requests_per_second", 2.0)  # 0.5s interval
        client = make_client([
            FakeResponse(200, {}),
            FakeResponse(429, headers={"Retry-After": "0"}),
            FakeResponse(200, {}),
        ], throttled=True)
        client._request("/a")
        client._request("/b")
        assert fake_time.sleeps == [0.5, 0.0, 0.5]  # throttle, backoff, throttle

    def test_transient_500_then_success(self, fake_time, no_jitter):
        ledger = FakeLedger()
        client = make_client([FakeResponse(500), FakeResponse(200, [{"id": "1"}])], ledger)
        assert client._request("/x") == [{"id": "1"}]
        assert [s for _, s in ledger.records] == [500, 200]

    def test_connection_error_retried(self, fake_time, no_jitter):
        ledger = FakeLedger()
        client = make_client(
            [requests.ConnectionError("boom"), FakeResponse(200, {"ok": 1})], ledger
        )
        assert client._request("/x") == {"ok": 1}
        assert [s for _, s in ledger.records] == [0, 200]  # network errors logged as 0

    def test_chunked_encoding_error_retried(self, fake_time, no_jitter):
        """Mid-body transport failures must be retried, not crash the run."""
        client = make_client([
            requests.exceptions.ChunkedEncodingError("connection dropped mid-body"),
            FakeResponse(200, {"ok": 1}),
        ])
        assert client._request("/x") == {"ok": 1}

    def test_timeout_exhausts_then_raises(self, fake_time, no_jitter, monkeypatch):
        monkeypatch.setattr(settings, "max_retries", 2)
        client = make_client([requests.Timeout("t")] * 3)
        with pytest.raises(RetriesExhausted):
            client._request("/x")
        assert len(client._session.calls) == 3

    def test_non_requests_exception_propagates(self, fake_time):
        client = make_client([RuntimeError("programming error")])
        with pytest.raises(RuntimeError):
            client._request("/x")

    def test_terminal_400_after_retried_500(self, fake_time, no_jitter):
        client = make_client([FakeResponse(500), FakeResponse(400, {"detail": "bad"})])
        with pytest.raises(RequestFailed) as exc_info:
            client._request("/x")
        assert exc_info.value.detail == "bad"
        assert len(client._session.calls) == 2

    def test_terminal_403_after_retried_500(self, fake_time, no_jitter):
        client = make_client([FakeResponse(500), FakeResponse(403)])
        with pytest.raises(AuthenticationError):
            client._request("/x")
        assert len(client._session.calls) == 2

    def test_429_exhaustion_burns_no_credits(self, fake_time, no_jitter, monkeypatch):
        monkeypatch.setattr(settings, "max_retries", 5)
        ledger = FakeLedger()
        client = make_client([FakeResponse(429)] * 6, ledger)
        with pytest.raises(RetriesExhausted):
            client._request("/x")
        assert [s for _, s in ledger.records] == [429] * 6
        assert ledger.used_this_month == 0


class TestStatusHandling:
    def test_403_raises_auth_error_without_retry(self, fake_time):
        client = make_client([FakeResponse(403)])
        with pytest.raises(AuthenticationError):
            client._request("/x")
        assert len(client._session.calls) == 1
        assert fake_time.sleeps == []

    def test_400_raises_request_failed_with_detail(self, fake_time):
        client = make_client([
            FakeResponse(400, {"detail": "Unsupported sort field 'created' for iOS reviews."})
        ])
        with pytest.raises(RequestFailed) as exc_info:
            client._request("/x")
        assert exc_info.value.status_code == 400
        assert "Unsupported sort field" in str(exc_info.value.detail)
        assert len(client._session.calls) == 1

    def test_422_validation_error_not_retried(self, fake_time):
        detail = [{"loc": ["query", "language"], "msg": "field required"}]
        client = make_client([FakeResponse(422, {"detail": detail})])
        with pytest.raises(RequestFailed) as exc_info:
            client._request("/x")
        assert exc_info.value.detail == detail

    def test_4xx_with_non_dict_json_body_still_raises_request_failed(self, fake_time):
        """A bare-list/str 4xx body must produce RequestFailed, not AttributeError."""
        client = make_client([FakeResponse(400, ["bad", "request"])])
        with pytest.raises(RequestFailed):
            client._request("/x")

    def test_4xx_with_non_json_body_uses_text(self, fake_time):
        client = make_client([FakeResponse(400, INVALID_JSON, text="Bad Request")])
        with pytest.raises(RequestFailed) as exc_info:
            client._request("/x")
        assert exc_info.value.detail == "Bad Request"

    def test_202_queued_for_crawl_returns_none(self, fake_time):
        client = make_client([FakeResponse(202)])
        assert client._request("/x") is None

    def test_204_no_data_returns_none(self, fake_time):
        client = make_client([FakeResponse(204)])
        assert client._request("/x") is None

    def test_invalid_json_200_retried_then_recovers(self, fake_time, no_jitter):
        client = make_client([
            FakeResponse(200, INVALID_JSON, text="<html>maintenance</html>"),
            FakeResponse(200, {"ok": 1}),
        ])
        assert client._request("/x") == {"ok": 1}
        assert len(fake_time.sleeps) == 1

    def test_invalid_json_200_exhausts_retries(self, fake_time, no_jitter, monkeypatch):
        monkeypatch.setattr(settings, "max_retries", 1)
        ledger = FakeLedger()
        client = make_client([FakeResponse(200, INVALID_JSON)] * 2, ledger)
        with pytest.raises(RetriesExhausted) as exc_info:
            client._request("/x")
        assert "invalid JSON" in str(exc_info.value)
        # the API answered 200 each time, so each attempt consumed a credit
        assert [s for _, s in ledger.records] == [200, 200]


# ------------------------------------------------------------ credit ledger


class TestCreditBudget:
    def test_exhausted_budget_blocks_before_any_http_call(self, fake_time):
        client = make_client([FakeResponse(200, {})], ledger=FakeLedger(exhausted=True))
        with pytest.raises(CreditBudgetExhausted):
            client._request("/x")
        assert client._session.calls == []

    def test_budget_checked_before_every_attempt(self, fake_time, no_jitter, monkeypatch):
        monkeypatch.setattr(settings, "max_retries", 2)
        ledger = FakeLedger()
        client = make_client([FakeResponse(500)] * 3, ledger)
        with pytest.raises(RetriesExhausted):
            client._request("/x")
        assert ledger.checks == 3


@pytest.fixture
def ledger_env(monkeypatch):
    """Real CreditLedger wired to a fake DB count and a no-op persistence."""
    db = {"count": 0, "loads": 0}

    def fake_load(self):
        db["loads"] += 1
        self.used_this_month = db["count"]
        self._requests_since_resync = 0

    class _NoopSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def add(self, row):
            pass

        def commit(self):
            pass

    monkeypatch.setattr(CreditLedger, "_load_from_db", fake_load)
    monkeypatch.setattr(mod, "SessionLocal", lambda: _NoopSession())
    monkeypatch.setattr(settings, "monthly_credit_budget", 100)
    monkeypatch.setattr(settings, "credit_safety_fraction", 0.95)
    return db


class TestCreditLedger:
    def test_threshold_is_fraction_of_budget(self, ledger_env):
        assert CreditLedger().threshold == 95

    def test_check_raises_at_threshold(self, ledger_env):
        ledger_env["count"] = 95
        ledger = CreditLedger()
        with pytest.raises(CreditBudgetExhausted):
            ledger.check()

    def test_check_passes_below_threshold(self, ledger_env):
        ledger_env["count"] = 94
        CreditLedger().check()  # must not raise

    def test_only_2xx_count_as_credits(self, ledger_env):
        ledger = CreditLedger()
        for status in (200, 202, 429, 500, 0, 400):
            ledger.record("/x", status, 5)
        assert ledger.used_this_month == 2  # 200 and 202

    def test_success_pushes_over_threshold(self, ledger_env):
        ledger_env["count"] = 94
        ledger = CreditLedger()
        ledger.record("/x", 200, 5)
        with pytest.raises(CreditBudgetExhausted):
            ledger.check()

    def test_resync_picks_up_concurrent_spend(self, ledger_env, monkeypatch):
        monkeypatch.setattr(CreditLedger, "RESYNC_EVERY", 5)
        ledger_env["count"] = 10
        ledger = CreditLedger()
        for _ in range(4):
            ledger.record("/x", 200, 5)
        ledger_env["count"] = 90  # another process spent heavily meanwhile
        ledger.check()
        assert ledger.used_this_month == 14  # below RESYNC_EVERY: no reload yet
        ledger.record("/x", 200, 5)  # 5th record crosses the resync threshold
        ledger.check()
        assert ledger.used_this_month == 90  # reloaded from shared ledger

    def test_month_rollover_resets_counter(self, ledger_env, monkeypatch):
        month = {"value": dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)}
        monkeypatch.setattr(mod, "month_start_utc", lambda now=None: month["value"])
        ledger_env["count"] = 96  # over threshold in July
        ledger = CreditLedger()
        with pytest.raises(CreditBudgetExhausted):
            ledger.check()
        # August: fresh budget must not raise a spurious CreditBudgetExhausted
        month["value"] = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
        ledger_env["count"] = 0
        ledger.check()  # must not raise
        assert ledger.used_this_month == 0
        assert ledger._month_start == month["value"]

    def test_month_start_utc_is_first_of_month_midnight(self):
        now = dt.datetime(2026, 7, 13, 17, 45, 12, 345, tzinfo=dt.timezone.utc)
        start = month_start_utc(now)
        assert start == dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)
        assert start.tzinfo is not None


class _StatefulLedgerDB:
    """Fake DB behind SessionLocal: counts committed 2xx rows, and can be put
    into a writes-fail/reads-succeed state (read-only replica, disk full)."""

    def __init__(self, count=0):
        self.count = count
        self.fail_writes = False
        db = self

        class _Session:
            def __init__(self):
                self.pending = []

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, query):
                return SimpleNamespace(scalar_one=lambda: db.count)

            def add(self, row):
                self.pending.append(row)

            def commit(self):
                if db.fail_writes:
                    raise RuntimeError("database is read-only")
                for row in self.pending:
                    if 200 <= row.status_code < 300:
                        db.count += 1
                self.pending.clear()

        self.session_cls = _Session


@pytest.fixture
def ledger_db(monkeypatch):
    db = _StatefulLedgerDB()
    monkeypatch.setattr(mod, "SessionLocal", lambda: db.session_cls())
    monkeypatch.setattr(settings, "monthly_credit_budget", 100)
    monkeypatch.setattr(settings, "credit_safety_fraction", 0.95)
    monkeypatch.setattr(CreditLedger, "RESYNC_EVERY", 5)
    return db


class TestLedgerFailsClosed:
    def test_guard_trips_even_when_db_writes_fail(self, ledger_db):
        """If ledger INSERTs fail but SELECTs succeed, resync must not wipe
        the in-memory spend — the budget guard has to fail closed."""
        ledger_db.count = 10
        ledger_db.fail_writes = True
        ledger = CreditLedger()
        with pytest.raises(CreditBudgetExhausted):
            for _ in range(200):  # far more than threshold; must trip long before
                ledger.check()
                ledger.record("/x", 200, 1)
        # 10 from DB + 85 unpersisted in-memory = 95 = threshold
        assert ledger.used_this_month == 95

    def test_healthy_db_still_trips_at_threshold(self, ledger_db):
        ledger_db.count = 10
        ledger = CreditLedger()
        with pytest.raises(CreditBudgetExhausted):
            for _ in range(200):
                ledger.check()
                ledger.record("/x", 200, 1)
        assert ledger.used_this_month == 95

    def test_month_rollover_clears_unpersisted_spend(self, ledger_db, monkeypatch):
        month = {"value": dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)}
        monkeypatch.setattr(mod, "month_start_utc", lambda now=None: month["value"])
        ledger_db.count = 10
        ledger_db.fail_writes = True
        ledger = CreditLedger()
        for _ in range(3):
            ledger.record("/x", 200, 1)
        assert ledger.used_this_month == 13
        month["value"] = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
        ledger_db.count = 0
        ledger.check()  # last month's unpersisted spend must not leak into August
        assert ledger.used_this_month == 0


# ------------------------------------------- endpoint wrappers / pagination


class TestEstimatesBatching:
    def test_ids_chunked_by_batch_size(self, fake_time, monkeypatch):
        monkeypatch.setattr(settings, "estimates_batch_size", 25)
        ids = [str(i) for i in range(30)]
        client = make_client([
            FakeResponse(200, [{"id": "0", "month": "2026-06", "revenue": 1}]),
            FakeResponse(200, [{"id": "25", "month": "2026-06", "revenue": 2}]),
        ])
        rows = client.get_estimates("ios", ids, start=dt.date(2026, 3, 1), end=dt.date(2026, 7, 1))
        assert len(client._session.calls) == 2
        first, second = client._session.calls
        assert first["params"]["id"] == ",".join(ids[:25])
        assert second["params"]["id"] == ",".join(ids[25:])
        assert first["params"]["start"] == "2026-03-01"
        assert first["params"]["end"] == "2026-07-01"
        assert [r["revenue"] for r in rows] == [1, 2]

    def test_no_data_chunk_does_not_crash_or_pollute(self, fake_time, monkeypatch):
        monkeypatch.setattr(settings, "estimates_batch_size", 1)
        client = make_client([FakeResponse(204), FakeResponse(200, [{"id": "b"}])])
        rows = client.get_estimates("play", ["a", "b"])
        assert rows == [{"id": "b"}]

    def test_empty_id_list_makes_no_requests(self, fake_time):
        client = make_client([])
        assert client.get_estimates("ios", []) == []
        assert client._session.calls == []


class TestReviewsParams:
    def test_ios_uses_country_and_date_sort(self, fake_time):
        client = make_client([FakeResponse(200, [])])
        client.get_reviews("ios", "553834731", country="US")
        params = client._session.calls[0]["params"]
        assert params["sort"] == "-date"
        assert params["country"] == "US"
        assert "language" not in params

    def test_play_uses_language_and_no_sort(self, fake_time):
        """Play's date sort 500s server-side — the client must never send sort."""
        client = make_client([FakeResponse(200, [])])
        client.get_reviews("play", "com.king.candycrushsaga")
        params = client._session.calls[0]["params"]
        assert params["language"] == settings.play_review_language
        assert "sort" not in params
        assert "country" not in params

    def test_unknown_store_rejected_without_http_call(self, fake_time):
        client = make_client([])
        with pytest.raises(ValueError):
            client.get_reviews("amazon", "x")
        assert client._session.calls == []

    def test_limit_clamped_to_api_maximum(self, fake_time):
        """The reviews endpoint accepts at most limit=1000 (422 above that);
        the client must clamp rather than send a doomed request."""
        client = make_client([FakeResponse(200, [])])
        client.get_reviews("ios", "553834731", limit=5000)
        assert client._session.calls[0]["params"]["limit"] == 1000

    def test_default_page_size_also_clamped(self, fake_time, monkeypatch):
        monkeypatch.setattr(settings, "reviews_page_size", 2000)
        client = make_client([FakeResponse(200, [])])
        client.get_reviews("ios", "553834731")
        assert client._session.calls[0]["params"]["limit"] == 1000

    def test_none_result_normalized_to_empty_list(self, fake_time):
        client = make_client([FakeResponse(204)])
        assert client.get_reviews("ios", "553834731") == []

    def test_limit_zero_fetches_nothing_and_costs_nothing(self, fake_time):
        client = make_client([])
        assert client.get_reviews("ios", "553834731", limit=0) == []
        assert client._session.calls == []


class TestOtherEndpointParams:
    def test_get_app_sends_country_default_and_joined_fields(self, fake_time):
        client = make_client([FakeResponse(200, {"id": "123", "name": "X"})])
        result = client.get_app("ios", "123", fields=["name", "rating_value"])
        call = client._session.calls[0]
        assert call["url"].endswith("/ios/apps/123")
        assert call["params"] == {"country": settings.default_country,
                                  "fields": "name,rating_value"}
        assert result == {"id": "123", "name": "X"}

    def test_get_app_queued_for_crawl_returns_none(self, fake_time):
        client = make_client([FakeResponse(202)])
        assert client.get_app("play", "com.new.app") is None

    def test_get_daily_installs_sends_iso_dates(self, fake_time):
        client = make_client([FakeResponse(200, [{"date": "2026-07-01", "ipd": 5}])])
        rows = client.get_daily_installs("com.x", start=dt.date(2026, 1, 1),
                                         end=dt.date(2026, 7, 1))
        call = client._session.calls[0]
        assert call["url"].endswith("/play/apps/com.x/installs_daily")
        assert call["params"] == {"start": "2026-01-01", "end": "2026-07-01"}
        assert rows == [{"date": "2026-07-01", "ipd": 5}]

    def test_get_daily_installs_no_data_returns_empty_list(self, fake_time):
        client = make_client([FakeResponse(204)])
        assert client.get_daily_installs("com.x") == []


class TestNewEndpoints:
    def test_rankings_single_page(self, fake_time):
        rows = [{"date": "2026-07-12", "app": "1", "collection": "Grossing", "rank": 1}]
        client = make_client([FakeResponse(200, {"data": rows, "total_count": 1})])
        result = client.get_rankings("ios", "US", "GAMES_PUZZLE",
                                     dt.date(2026, 7, 10), dt.date(2026, 7, 12))
        assert result == rows
        params = client._session.calls[0]["params"]
        assert params["category"] == "GAMES_PUZZLE"
        assert params["rank_end"] == 200
        assert params["page"] == 1

    def test_rankings_paginates_until_total(self, fake_time, monkeypatch):
        monkeypatch.setattr(settings, "rankings_page_size", 2)
        page1 = [{"date": "2026-07-12", "app": "a", "rank": 1},
                 {"date": "2026-07-12", "app": "b", "rank": 2}]
        page2 = [{"date": "2026-07-12", "app": "c", "rank": 3}]
        client = make_client([
            FakeResponse(200, {"data": page1, "total_count": 3}),
            FakeResponse(200, {"data": page2, "total_count": 3}),
        ])
        result = client.get_rankings("play", "US", "GAME_PUZZLE",
                                     dt.date(2026, 7, 12), dt.date(2026, 7, 12))
        assert [r["app"] for r in result] == ["a", "b", "c"]
        assert [c["params"]["page"] for c in client._session.calls] == [1, 2]

    def test_rankings_none_response_is_empty(self, fake_time):
        client = make_client([FakeResponse(204)])
        assert client.get_rankings("ios", "US", "GAMES_PUZZLE",
                                   dt.date(2026, 7, 12), dt.date(2026, 7, 12)) == []

    def test_query_apps_posts_filter_body(self, fake_time):
        client = make_client([FakeResponse(200, {"data": [{"id": "com.x"}]})])
        result = client.query_apps("play", {"developer_id": "123"},
                                   fields=["id", "name", "release_date"],
                                   sort="-release_date", limit=50)
        call = client._session.calls[0]
        assert call["method"] == "POST"
        assert call["url"].endswith("/play/apps/query")
        assert call["json"] == {"filter": {"developer_id": "123"},
                                "fields": ["id", "name", "release_date"],
                                "sort": "-release_date", "limit": 50, "page": 1}
        assert result == [{"id": "com.x"}]

    def test_query_apps_no_data_returns_empty(self, fake_time):
        client = make_client([FakeResponse(204)])
        assert client.query_apps("ios", {"developer_id": "1"}) == []

    def test_get_summary_posts_filter(self, fake_time):
        summary = {"total": 10, "revenue": 5, "downloads": 7,
                   "available": 3, "removed": 7, "ipd": 2}
        client = make_client([FakeResponse(200, summary)])
        result = client.get_summary("play", {"category": "GAME_PUZZLE",
                                             "downloads_month": {"from": 100000}})
        call = client._session.calls[0]
        assert call["method"] == "POST"
        assert call["json"] == {"filter": {"category": "GAME_PUZZLE",
                                           "downloads_month": {"from": 100000}}}
        assert result == summary

    def test_get_developer(self, fake_time):
        client = make_client([FakeResponse(200, {"id": "123", "name": "King"})])
        assert client.get_developer("play", "123") == {"id": "123", "name": "King"}
        assert client._session.calls[0]["url"].endswith("/play/developers/123")

    def test_search_apps_params(self, fake_time):
        client = make_client([FakeResponse(200, [{"id": "1", "name": "Royal Match"}])])
        result = client.search_apps("ios", "Royal Match", limit=1,
                                    fields=("id", "name", "developer_id"))
        params = client._session.calls[0]["params"]
        assert params["q"] == "Royal Match"
        assert params["fields"] == "id,name,developer_id"
        assert params["country"] == settings.default_country
        assert result[0]["name"] == "Royal Match"

    def test_post_retried_on_transient_500(self, fake_time, no_jitter):
        client = make_client([FakeResponse(500),
                              FakeResponse(200, {"total": 1})])
        assert client.get_summary("ios", {"category": "GAMES_PUZZLE"}) == {"total": 1}
        assert [c["method"] for c in client._session.calls] == ["POST", "POST"]
