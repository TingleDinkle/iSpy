"""Game-focused review topic classification.

Keyword-rule based (fast, free, transparent). Single-word keywords match on
word boundaries ("bug" does not match "bugatti"); multi-word phrases match as
substrings. Edit TOPICS to tune — re-tag history afterwards with:
    python analyze_reviews.py --retag
"""

from __future__ import annotations

import math
import re
from functools import lru_cache
from typing import Optional

# topic -> list of keywords/phrases (lowercase)
TOPICS: dict[str, list[str]] = {
    "crash_bug": [
        "crash", "crashes", "crashing", "freeze", "freezes", "frozen",
        "black screen", "won't load", "wont load", "not loading", "won't open",
        "wont open", "stuck on loading", "bug", "bugs", "buggy", "glitch",
        "glitches", "glitchy", "laggy", "lags", "keeps closing",
    ],
    "monetization": [
        "pay to win", "pay-to-win", "p2w", "cash grab", "money grab",
        "too expensive", "overpriced", "microtransaction", "micro transaction",
        "paywall", "pay wall", "greedy", "milking", "whales", "gacha rates",
        "predatory",
    ],
    "ads_complaints": [
        "too many ads", "ads after every", "ad after every", "forced ads",
        "unskippable", "ads every", "constant ads", "full of ads",
        "more ads than game", "30 second ad",
    ],
    "difficulty": [
        "too hard", "too difficult", "impossible level", "impossible to beat",
        "unfair", "difficulty spike", "rigged", "too easy", "way too hard",
        "can't beat", "cant beat", "designed to make you fail",
    ],
    "progression_grind": [
        "grind", "grindy", "grinding", "slow progress", "takes forever",
        "energy system", "lives system", "out of lives", "wait hours",
        "progress wall", "level cap",
    ],
    "content_drought": [
        "no new levels", "ran out of levels", "nothing to do", "need more content",
        "more levels please", "waiting for new levels", "same thing over and over",
        "repetitive", "gets boring",
    ],
    "multiplayer_matchmaking": [
        "matchmaking", "unbalanced match", "cheater", "cheaters", "hacker",
        "hackers", "bots", "full of bots", "pvp is unfair", "always matched against",
    ],
    "controls_ux": [
        "controls", "clunky", "unresponsive", "hard to see", "confusing menu",
        "ui is", "hitbox", "touch doesn't register", "touch doesnt register",
    ],
    "account_data_loss": [
        "lost my progress", "lost progress", "lost all my", "account reset",
        "data loss", "restore purchase", "didn't receive", "didnt receive",
        "never got my", "charged me",
    ],
    "praise": [
        "love this game", "love it", "best game", "so addictive", "amazing game",
        "great game", "so much fun", "highly recommend", "can't stop playing",
        "cant stop playing",
    ],
}


@lru_cache(maxsize=1)
def _compiled() -> list[tuple[str, re.Pattern]]:
    patterns: list[tuple[str, re.Pattern]] = []
    for topic, keywords in TOPICS.items():
        parts = []
        for kw in keywords:
            escaped = re.escape(kw.lower())
            if " " in kw or "-" in kw:
                parts.append(escaped)          # phrases: substring match
            else:
                parts.append(rf"\b{escaped}\b")  # single words: whole-word match
        patterns.append((topic, re.compile("|".join(parts))))
    return patterns


def classify(text: Optional[str]) -> list[str]:
    """Return the sorted list of topics present in a review text."""
    if not text:
        return []
    lowered = text.lower()
    return sorted(topic for topic, pattern in _compiled() if pattern.search(lowered))


def review_weight(likes: Optional[int]) -> float:
    """Helpfulness weighting: a review with 1000 likes counts ~4x a fresh one."""
    return 1.0 + math.log10(1 + max(likes or 0, 0))
