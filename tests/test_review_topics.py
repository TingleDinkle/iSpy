"""Unit tests for game-review topic classification and helpfulness weighting."""

from __future__ import annotations

import pytest

from tracker.review_topics import TOPICS, classify, review_weight
from analyze_reviews import weighted_avg_stars


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
