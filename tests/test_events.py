"""Unit tests for snapshot diffing, soft-launch heuristics, and rank-move
analysis (tracker/events.py) — all pure logic."""

from __future__ import annotations

import pytest

from tracker.config import settings
from tracker.events import (
    CHURNY_COLLECTIONS,
    EventDraft,
    analyze_rank_moves,
    diff_snapshots,
    entry_top_for_collection,
    looks_like_soft_launch,
)


def types(drafts: list[EventDraft]) -> list[str]:
    return sorted(d.event_type for d in drafts)


BASE = {
    "version": "1.0.0",
    "whatsnew": "Initial release",
    "icon": "https://cdn/icon-v1.png",
    "screenshots": ["https://cdn/s1.png", "https://cdn/s2.png"],
    "advertised": False,
    "countries_list": ["US", "GB", "DE", "FR"],
    "developer_id": "111",
    "developer_name": "Original Studio",
    "top_countries_revenue": ["US", "GB", "CA", "AU", "DE"],
    "description": "A wonderful match-3 adventure through candy kingdoms.",
    "ads": {"video": {"url": "https://cdn/ad-v1.mp4"},
            "image": {"url": "https://cdn/ad-i1.jpg"}},
}


class TestDiffSnapshots:
    def test_no_change_yields_no_events(self):
        assert diff_snapshots(dict(BASE), dict(BASE), "Game") == []

    def test_missing_side_yields_no_events(self):
        assert diff_snapshots(None, dict(BASE), "Game") == []
        assert diff_snapshots(dict(BASE), None, "Game") == []
        assert diff_snapshots({}, dict(BASE), "Game") == []

    def test_version_update_carries_patch_notes(self):
        curr = {**BASE, "version": "1.1.0", "whatsnew": "New event: Summer Splash!"}
        drafts = diff_snapshots(dict(BASE), curr, "Royal Match")
        assert types(drafts) == ["version_update"]
        d = drafts[0]
        assert "1.1.0" in d.title and "Royal Match" in d.title
        assert d.details["whatsnew"] == "New event: Summer Splash!"
        assert d.details["old_version"] == "1.0.0"

    def test_long_whatsnew_is_clipped(self):
        curr = {**BASE, "version": "1.1.0", "whatsnew": "x" * 5000}
        drafts = diff_snapshots(dict(BASE), curr, "G")
        assert len(drafts[0].details["whatsnew"]) <= 801  # 800 + ellipsis

    def test_version_change_needs_both_sides(self):
        # A previously-missing version must not fire an event.
        prev = {**BASE, "version": None}
        curr = {**BASE, "version": "2.0"}
        assert diff_snapshots(prev, curr, "G") == []

    def test_icon_change(self):
        curr = {**BASE, "icon": "https://cdn/icon-v2.png"}
        drafts = diff_snapshots(dict(BASE), curr, "G")
        assert types(drafts) == ["icon_change"]

    def test_screenshots_reorder_only_is_not_an_event(self):
        # CDN/A-B rotation reorders the same set — that's noise, not a change
        curr = {**BASE, "screenshots": list(reversed(BASE["screenshots"]))}
        assert diff_snapshots(dict(BASE), curr, "G") == []

    def test_screenshots_change_counts_delta(self):
        curr = {**BASE, "screenshots": ["https://cdn/s2.png", "https://cdn/s3.png",
                                        "https://cdn/s4.png"]}
        drafts = diff_snapshots(dict(BASE), curr, "G")
        assert types(drafts) == ["screenshots_change"]
        assert drafts[0].details == {"added": 2, "removed": 1,
                                     "old_count": 2, "new_count": 3}

    def test_ua_start_and_stop(self):
        started = diff_snapshots(dict(BASE), {**BASE, "advertised": True}, "G")
        assert types(started) == ["ua_start"]
        stopped = diff_snapshots({**BASE, "advertised": True}, dict(BASE), "G")
        assert types(stopped) == ["ua_stop"]

    def test_missing_advertised_field_is_not_a_flip(self):
        prev = {k: v for k, v in BASE.items() if k != "advertised"}
        assert diff_snapshots(prev, {**BASE, "advertised": True}, "G") == []

    def test_countries_expanded_and_reduced(self):
        curr = {**BASE, "countries_list": ["US", "GB", "DE", "JP", "KR"]}
        drafts = diff_snapshots(dict(BASE), curr, "G")
        assert types(drafts) == ["countries_expanded", "countries_reduced"]
        expanded = next(d for d in drafts if d.event_type == "countries_expanded")
        assert expanded.details["added"] == ["JP", "KR"]

    def test_global_launch_from_soft_launch(self):
        prev = {**BASE, "countries_list": ["PH", "CA", "AU", "NZ"]}
        curr = {**BASE, "countries_list": ["PH", "CA", "AU", "NZ", "US", "GB", "DE"]}
        drafts = diff_snapshots(prev, curr, "Merge Mansion")
        assert types(drafts) == ["global_launch"]
        assert "GLOBAL" in drafts[0].title

    def test_expansion_without_soft_launch_prior_is_plain_expansion(self):
        prev = {**BASE, "countries_list": ["US", "GB"]}  # already global
        curr = {**BASE, "countries_list": ["US", "GB", "JP"]}
        drafts = diff_snapshots(prev, curr, "G")
        assert types(drafts) == ["countries_expanded"]

    def test_multiple_simultaneous_changes_all_reported(self):
        curr = {**BASE, "version": "1.1", "icon": "https://cdn/new.png",
                "advertised": True}
        drafts = diff_snapshots(dict(BASE), curr, "G")
        assert types(drafts) == ["icon_change", "ua_start", "version_update"]


class TestBusinessSignalDiffs:
    def test_app_transfer_detected(self):
        curr = {**BASE, "developer_id": "222", "developer_name": "Acquirer Corp"}
        drafts = diff_snapshots(dict(BASE), curr, "Sold Game")
        assert types(drafts) == ["app_transferred"]
        assert "Original Studio" in drafts[0].title
        assert "Acquirer Corp" in drafts[0].title

    def test_same_developer_no_transfer(self):
        assert diff_snapshots(dict(BASE), dict(BASE), "G") == []

    def test_geo_leader_change_reported(self):
        curr = {**BASE, "top_countries_revenue": ["JP", "US", "GB", "CA", "AU"]}
        drafts = diff_snapshots(dict(BASE), curr, "G")
        assert types(drafts) == ["geo_revenue_shift"]
        assert "new top market JP" in drafts[0].title

    def test_geo_membership_change_reported(self):
        curr = {**BASE, "top_countries_revenue": ["US", "GB", "CA", "AU", "KR"]}
        drafts = diff_snapshots(dict(BASE), curr, "G")
        assert types(drafts) == ["geo_revenue_shift"]
        assert drafts[0].details["new_top5"][-1] == "KR"

    def test_geo_reorder_below_leader_is_quiet(self):
        # 2nd and 3rd place swapping is estimate jitter, not a shift
        curr = {**BASE, "top_countries_revenue": ["US", "CA", "GB", "AU", "DE"]}
        assert diff_snapshots(dict(BASE), curr, "G") == []

    def test_listing_rewrite_reported_with_lengths(self):
        curr = {**BASE, "description": "Now with WEEKLY EVENTS! " * 10}
        drafts = diff_snapshots(dict(BASE), curr, "G")
        assert types(drafts) == ["listing_change"]
        assert drafts[0].details["old_length"] == len(BASE["description"])

    def test_ad_creative_rotation_reported_with_urls(self):
        curr = {**BASE, "ads": {"video": {"url": "https://cdn/ad-v2.mp4"},
                                "image": {"url": "https://cdn/ad-i1.jpg"}}}
        drafts = diff_snapshots(dict(BASE), curr, "G")
        assert types(drafts) == ["ad_creative_change"]
        assert drafts[0].details == {"video": {"old": "https://cdn/ad-v1.mp4",
                                               "new": "https://cdn/ad-v2.mp4"}}

    def test_play_boolean_ads_field_never_diffs(self):
        prev = {**BASE, "ads": False}
        curr = {**BASE, "ads": True}
        assert diff_snapshots(prev, curr, "G") == []

    def test_first_appearance_of_new_fields_is_not_an_event(self):
        # transition day: older snapshots lack the newly-fetched fields
        prev = {k: v for k, v in BASE.items()
                if k not in ("top_countries_revenue", "description", "ads",
                             "developer_id")}
        assert diff_snapshots(prev, dict(BASE), "G") == []


class TestSoftLaunchHeuristic:
    def test_small_test_market_set_is_soft_launch(self):
        assert looks_like_soft_launch(["PH", "CA", "AU", "NZ"])

    def test_us_presence_disqualifies(self):
        assert not looks_like_soft_launch(["US", "PH", "CA"])

    def test_gb_presence_disqualifies(self):
        assert not looks_like_soft_launch(["GB", "PH"])

    def test_wide_release_disqualifies(self):
        countries = ["PH", "CA", "AU", "NZ", "ID", "MY", "SG", "IE", "NL",
                     "DK", "SE", "NO", "FI", "TH", "VN", "MX"]  # 16 > 15
        assert not looks_like_soft_launch(countries)

    def test_empty_or_none_is_not_soft_launch(self):
        assert not looks_like_soft_launch([])
        assert not looks_like_soft_launch(None)

    def test_case_insensitive(self):
        assert not looks_like_soft_launch(["us", "ph"])


class TestRankMoves:
    def test_chart_entry_from_outside(self):
        prev = {"a": 1, "b": 2}
        curr = {"a": 1, "b": 2, "newcomer": 30}
        moves = analyze_rank_moves(prev, curr, "chart", entry_top=50, jump_min=20)
        assert [(app, d.event_type) for app, d in moves] == [("newcomer", "chart_entry")]
        assert "outside the chart" in moves[0][1].title

    def test_chart_entry_from_below_threshold(self):
        prev = {"a": 1, "riser": 80}
        curr = {"a": 1, "riser": 45}
        moves = analyze_rank_moves(prev, curr, "chart", entry_top=50, jump_min=20)
        assert [(app, d.event_type) for app, d in moves] == [("riser", "chart_entry")]
        assert "#80" in moves[0][1].title

    def test_rank_jump_within_chart(self):
        prev = {"a": 1, "jumper": 90}
        curr = {"a": 1, "jumper": 60}  # +30, still outside top 50
        moves = analyze_rank_moves(prev, curr, "chart", entry_top=50, jump_min=20)
        assert [(app, d.event_type) for app, d in moves] == [("jumper", "rank_jump")]

    def test_small_moves_are_ignored(self):
        prev = {"a": 1, "b": 60}
        curr = {"a": 1, "b": 55}  # +5 < 20
        assert analyze_rank_moves(prev, curr, "chart", entry_top=50, jump_min=20) == []

    def test_deep_chart_jumps_are_noise(self):
        # #180 -> #150 gained 30 but stays far from the top — no event.
        prev = {"a": 1, "deep": 180}
        curr = {"a": 1, "deep": 150}
        assert analyze_rank_moves(prev, curr, "chart", entry_top=50, jump_min=20) == []

    def test_leader_change(self):
        prev = {"old_king": 1, "challenger": 2}
        curr = {"old_king": 2, "challenger": 1}
        moves = analyze_rank_moves(prev, curr, "chart", entry_top=50, jump_min=20)
        assert [d.event_type for _, d in moves] == ["chart_leader_change"]
        assert "challenger" in moves[0][1].title

    def test_names_used_in_titles(self):
        prev = {"id1": 1, "id2": 2}
        curr = {"id1": 2, "id2": 1}
        moves = analyze_rank_moves(prev, curr, "chart",
                                   names={"id2": "Royal Match", "id1": "Candy Crush"},
                                   entry_top=50, jump_min=20)
        assert "Royal Match" in moves[0][1].title
        assert "Candy Crush" in moves[0][1].title

    def test_empty_sides_yield_nothing(self):
        assert analyze_rank_moves({}, {"a": 1}, "chart") == []
        assert analyze_rank_moves({"a": 1}, {}, "chart") == []

    def test_falling_off_the_chart_is_not_an_event(self):
        prev = {"a": 1, "faller": 40}
        curr = {"a": 1}
        moves = analyze_rank_moves(prev, curr, "chart", entry_top=50, jump_min=20)
        assert moves == []


class TestChurnyChartThresholds:
    def test_churny_collections_get_tight_entry_bar(self):
        for collection in CHURNY_COLLECTIONS:
            assert entry_top_for_collection(collection) == settings.rank_churny_entry_top

    def test_stable_collections_keep_the_wide_bar(self):
        for collection in ("Grossing", "Free", "Paid", "topgrossing", "topselling_free"):
            assert entry_top_for_collection(collection) == settings.rank_entry_top

    def test_movers_entry_at_40_suppressed_but_top10_reported(self):
        entry_top = entry_top_for_collection("movers_shakers")
        prev = {"a": 1}
        churn = {"a": 1, "noise": 40}
        assert analyze_rank_moves(prev, churn, "movers", entry_top=entry_top,
                                  jump_min=20) == []
        breakout = {"a": 1, "star": 7}
        moves = analyze_rank_moves(prev, breakout, "movers", entry_top=entry_top,
                                   jump_min=20)
        assert [(app, d.event_type) for app, d in moves] == [("star", "chart_entry")]
