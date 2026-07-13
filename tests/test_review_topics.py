"""Unit tests for game-review topic classification and helpfulness weighting."""

from __future__ import annotations

import pytest

import datetime as dt

from tracker.review_topics import TOPICS, classify, review_weight
from analyze_reviews import bucket_for, weighted_avg_stars

TODAY = dt.date(2026, 7, 13)
UTC = dt.timezone.utc


class TestWindowBuckets:
    def test_windows_are_symmetric_seven_days(self):
        # recent = (today-7, today], prior = (today-14, today-7]
        assert bucket_for(dt.datetime(2026, 7, 13, 12, tzinfo=UTC), TODAY, 7) == "recent"
        assert bucket_for(dt.datetime(2026, 7, 7, 0, 1, tzinfo=UTC), TODAY, 7) == "recent"
        assert bucket_for(dt.datetime(2026, 7, 6, 23, 59, tzinfo=UTC), TODAY, 7) == "prior"
        assert bucket_for(dt.datetime(2026, 6, 30, 0, 1, tzinfo=UTC), TODAY, 7) == "prior"
        assert bucket_for(dt.datetime(2026, 6, 29, 23, 59, tzinfo=UTC), TODAY, 7) is None

    def test_recent_and_prior_cover_equal_day_counts(self):
        days = [TODAY - dt.timedelta(days=i) for i in range(0, 20)]
        buckets = [bucket_for(dt.datetime.combine(d, dt.time(12), tzinfo=UTC), TODAY, 7)
                   for d in days]
        assert buckets.count("recent") == 7
        assert buckets.count("prior") == 7

    def test_local_timezone_timestamp_normalized_to_utc(self):
        # 23:30 on the boundary day in UTC+7 is 16:30 UTC the SAME day — but a
        # naive .date() in local time would land it a day late.
        bangkok = dt.timezone(dt.timedelta(hours=7))
        ts = dt.datetime(2026, 7, 7, 5, 30, tzinfo=bangkok)  # = 2026-07-06 22:30 UTC
        assert bucket_for(ts, TODAY, 7) == "prior"

    def test_naive_timestamp_treated_as_utc(self):
        assert bucket_for(dt.datetime(2026, 7, 13, 1, 0), TODAY, 7) == "recent"


class TestClassify:
    def test_crash_complaint(self):
        assert "crash_bug" in classify("The game crashes every time I open a chest")

    def test_word_boundary_no_false_positive(self):
        # 'bug' must not match inside 'bugatti'
        assert classify("I love driving the Bugatti in this racing game") == []

    def test_multiword_phrase(self):
        assert "monetization" in classify("This is just a cash grab, pure pay to win")

    def test_multiple_topics(self):
        topics = classify("Love this game but it crashes after every ad and "
                          "there are too many ads")
        assert "crash_bug" in topics
        assert "ads_complaints" in topics
        assert "praise" in topics

    def test_difficulty_and_grind(self):
        topics = classify("Level 847 is an impossible level, and the energy system "
                          "makes progress so grindy")
        assert "difficulty" in topics
        assert "progression_grind" in topics

    def test_data_loss(self):
        assert "account_data_loss" in classify("Lost my progress after the update!!")

    def test_case_insensitive(self):
        assert "crash_bug" in classify("CONSTANTLY CRASHES ON MY PHONE")

    def test_empty_and_none(self):
        assert classify("") == []
        assert classify(None) == []

    def test_neutral_text_untagged(self):
        assert classify("It's a match three game with colorful candies.") == []

    def test_output_sorted_and_unique(self):
        topics = classify("crash crash crash bug glitch")
        assert topics == sorted(set(topics))

    def test_every_topic_reachable(self):
        # each topic's first keyword must classify into that topic
        for topic, keywords in TOPICS.items():
            assert topic in classify(f"honestly {keywords[0]} basically"), topic


class TestWeighting:
    def test_no_likes_weight_one(self):
        assert review_weight(None) == pytest.approx(1.0)
        assert review_weight(0) == pytest.approx(1.0)

    def test_thousand_likes_roughly_4x(self):
        assert review_weight(999) == pytest.approx(4.0, abs=0.01)

    def test_negative_likes_clamped(self):
        assert review_weight(-5) == pytest.approx(1.0)

    def test_weighted_average_pulls_toward_liked_reviews(self):
        # one 1-star review with 999 likes vs three 5-star with none:
        # weighted avg = (1*4 + 5*3) / (4 + 3) ≈ 2.71 — far below the raw 4.0
        rows = [(1, 999), (5, 0), (5, 0), (5, 0)]
        assert weighted_avg_stars(rows) == pytest.approx(2.714, abs=0.01)

    def test_weighted_average_ignores_null_stars(self):
        assert weighted_avg_stars([(None, 50), (4, 0)]) == pytest.approx(4.0)

    def test_weighted_average_empty(self):
        assert weighted_avg_stars([]) is None
        assert weighted_avg_stars([(None, 10)]) is None
