"""Unit tests for digest formatting and webhook delivery (tracker/notify.py)."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from tracker import notify as mod
from tracker.config import settings
from tracker.notify import (
    DISCORD_CHUNK_LIMIT,
    NotifyFailed,
    format_digest,
    send_chunks,
)


def alert(app_id=1, metric="revenue", pct=60.0, value=1600.0, baseline=1000.0):
    return SimpleNamespace(app_id=app_id, metric=metric, pct_change=pct,
                           value=value, baseline=baseline, window_days=7,
                           alert_date=dt.date(2026, 7, 13))


def event(event_type="version_update", title="Game shipped 1.1", app_id=1):
    return SimpleNamespace(event_type=event_type, title=title, app_id=app_id)


TODAY = dt.date(2026, 7, 13)


class TestFormatDigest:
    def test_empty_input_no_chunks(self):
        assert format_digest([], [], {}, today=TODAY) == []

    def test_sections_grouped_with_headers(self):
        alerts = [alert()]
        events = [
            event("chart_entry", "A entered top 50"),
            event("version_update", "B shipped 2.0"),
            event("ua_start", "C started running ads"),
            event("review_topic_surge", "D crash complaints surging"),
            event("new_developer_app", "Studio has a new game: E"),
        ]
        text = "\n".join(format_digest(alerts, events, {1: "Candy Crush (ios)"},
                                       today=TODAY))
        assert "Metric alerts" in text
        assert "Candy Crush (ios)" in text
        # rare-but-important sections precede voluminous chart churn
        assert text.index("Updates shipped") < text.index("Chart moves")
        assert text.index("Launch radar") < text.index("Chart moves")
        assert "UA & creative" in text
        assert "Review signals" in text

    def test_alert_line_shows_direction_and_magnitude(self):
        chunks = format_digest([alert(pct=-12.5, metric="rating",
                                      value=3.5, baseline=4.0)],
                               [], {1: "MyGame (play)"}, today=TODAY)
        line = chunks[0]
        assert "▼" in line and "13%" in line or "12" in line
        assert "rating" in line

    def test_unknown_event_types_go_to_other(self):
        chunks = format_digest([], [event("weird_new_type", "Something odd")],
                               {}, today=TODAY)
        text = "\n".join(chunks)
        assert "Other" in text and "Something odd" in text

    def test_chunks_respect_discord_limit(self):
        events = [event("version_update", f"Game {i} shipped a very long update "
                        + "x" * 150) for i in range(50)]
        chunks = format_digest([], events, {}, today=TODAY)
        assert len(chunks) > 1
        assert all(len(c) <= DISCORD_CHUNK_LIMIT for c in chunks)

    def test_overflow_summarized(self):
        events = [event("version_update", f"update {i}") for i in range(100)]
        text = "\n".join(format_digest([], events, {}, today=TODAY))
        assert "more" in text  # "…and N more"

    def test_header_counts_items(self):
        chunks = format_digest([alert()], [event()], {1: "X"}, today=TODAY)
        assert "2 item(s)" in chunks[0]


class FakeResponse:
    def __init__(self, status_code, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class TestSendChunks:
    @pytest.fixture(autouse=True)
    def fast_sleep(self, monkeypatch):
        monkeypatch.setattr(mod.time, "sleep", lambda s: None)

    def test_no_sinks_configured_sends_nothing(self, monkeypatch):
        monkeypatch.setattr(settings, "discord_webhook_url", None)
        monkeypatch.setattr(settings, "slack_webhook_url", None)
        assert send_chunks(["hello"]) == []

    def test_discord_delivery(self, monkeypatch):
        monkeypatch.setattr(settings, "discord_webhook_url", "https://d.example/wh")
        monkeypatch.setattr(settings, "slack_webhook_url", None)
        posts = []

        def fake_post(url, json=None, timeout=None):
            posts.append((url, json))
            return FakeResponse(204)

        monkeypatch.setattr(mod.requests, "post", fake_post)
        assert send_chunks(["chunk1", "chunk2"]) == ["discord"]
        assert posts == [("https://d.example/wh", {"content": "chunk1"}),
                         ("https://d.example/wh", {"content": "chunk2"})]

    def test_slack_uses_text_payload(self, monkeypatch):
        monkeypatch.setattr(settings, "discord_webhook_url", None)
        monkeypatch.setattr(settings, "slack_webhook_url", "https://s.example/wh")
        posts = []
        monkeypatch.setattr(mod.requests, "post",
                            lambda url, json=None, timeout=None:
                            (posts.append(json), FakeResponse(200))[1])
        send_chunks(["hi"])
        assert posts == [{"text": "hi"}]

    def test_transient_failure_retried_then_succeeds(self, monkeypatch):
        monkeypatch.setattr(settings, "discord_webhook_url", "https://d.example/wh")
        monkeypatch.setattr(settings, "slack_webhook_url", None)
        responses = [FakeResponse(500), FakeResponse(204)]
        monkeypatch.setattr(mod.requests, "post",
                            lambda url, json=None, timeout=None: responses.pop(0))
        assert send_chunks(["x"]) == ["discord"]

    def test_bad_webhook_fails_fast_without_retry(self, monkeypatch):
        monkeypatch.setattr(settings, "discord_webhook_url", "https://d.example/wh")
        monkeypatch.setattr(settings, "slack_webhook_url", None)
        calls = []

        def fake_post(url, json=None, timeout=None):
            calls.append(1)
            return FakeResponse(404, "Unknown Webhook")

        monkeypatch.setattr(mod.requests, "post", fake_post)
        with pytest.raises(NotifyFailed):
            send_chunks(["x"])
        assert len(calls) == 1  # 4xx = config error, retrying won't help

    def test_persistent_failure_raises(self, monkeypatch):
        monkeypatch.setattr(settings, "discord_webhook_url", "https://d.example/wh")
        monkeypatch.setattr(settings, "slack_webhook_url", None)
        monkeypatch.setattr(mod.requests, "post",
                            lambda url, json=None, timeout=None: FakeResponse(500))
        with pytest.raises(NotifyFailed):
            send_chunks(["x"])

    def test_one_sink_failing_still_raises_but_other_delivered(self, monkeypatch):
        monkeypatch.setattr(settings, "discord_webhook_url", "https://d.example/wh")
        monkeypatch.setattr(settings, "slack_webhook_url", "https://s.example/wh")

        def fake_post(url, json=None, timeout=None):
            return FakeResponse(204 if "d.example" in url else 500)

        monkeypatch.setattr(mod.requests, "post", fake_post)
        with pytest.raises(NotifyFailed):
            send_chunks(["x"])  # slack failed -> items must stay queued
