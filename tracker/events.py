"""Pure event-detection logic: snapshot diffs, soft-launch heuristics, and
chart-movement analysis. No database or HTTP access — everything here takes
plain dicts/lists and returns EventDraft objects, so it is fully unit-testable.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import settings

# Countries commonly used for soft launches (test markets before global).
SOFT_LAUNCH_MARKERS = {"PH", "CA", "AU", "NZ", "ID", "MY", "SG", "IE", "NL", "DK",
                       "SE", "NO", "FI", "TH", "VN", "MX", "BR", "TR", "PL"}
GLOBAL_MARKERS = {"US", "GB"}


@dataclass
class EventDraft:
    event_type: str
    title: str
    details: dict[str, Any] = field(default_factory=dict)


def store_url(store: str, store_app_id: str) -> str:
    """Public store-page URL — used as the label for untracked chart apps so
    the digest links straight to the app instead of showing a bare id."""
    if store == "ios":
        return f"https://apps.apple.com/app/id{store_app_id}"
    return f"https://play.google.com/store/apps/details?id={store_app_id}"


def looks_like_soft_launch(countries: Optional[list[str]]) -> bool:
    """Small storefront set without the big global markets = likely soft launch."""
    if not countries:
        return False
    country_set = {c.upper() for c in countries}
    return (
        len(country_set) <= settings.soft_launch_max_countries
        and not (country_set & GLOBAL_MARKERS)
    )


def _clip(text: Optional[str], limit: int = 800) -> Optional[str]:
    if text is None:
        return None
    text = str(text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def diff_snapshots(
    prev_raw: Optional[dict[str, Any]],
    curr_raw: Optional[dict[str, Any]],
    app_name: str,
) -> list[EventDraft]:
    """Compare two daily app-details payloads and describe what changed.

    Detects: version updates (with patch notes), icon changes, screenshot-set
    changes, UA (advertised) flips, storefront expansion/reduction, and the
    soft-launch -> global-launch transition.
    """
    if not prev_raw or not curr_raw:
        return []
    drafts: list[EventDraft] = []

    prev_version, curr_version = prev_raw.get("version"), curr_raw.get("version")
    if curr_version and prev_version and curr_version != prev_version:
        drafts.append(EventDraft(
            "version_update",
            f"{app_name} shipped {curr_version} (was {prev_version})",
            {"old_version": prev_version, "new_version": curr_version,
             "whatsnew": _clip(curr_raw.get("whatsnew"))},
        ))

    prev_icon, curr_icon = prev_raw.get("icon"), curr_raw.get("icon")
    if curr_icon and prev_icon and curr_icon != prev_icon:
        drafts.append(EventDraft(
            "icon_change",
            f"{app_name} changed its store icon",
            {"old_icon": prev_icon, "new_icon": curr_icon},
        ))

    prev_shots = prev_raw.get("screenshots") or []
    curr_shots = curr_raw.get("screenshots") or []
    # compare as sets: order-only rotation is CDN noise, not a creative change
    if prev_shots and curr_shots and set(prev_shots) != set(curr_shots):
        added = len(set(curr_shots) - set(prev_shots))
        removed = len(set(prev_shots) - set(curr_shots))
        drafts.append(EventDraft(
            "screenshots_change",
            f"{app_name} updated store screenshots (+{added}/-{removed}, "
            f"{len(curr_shots)} total)",
            {"added": added, "removed": removed,
             "old_count": len(prev_shots), "new_count": len(curr_shots)},
        ))

    prev_ua, curr_ua = prev_raw.get("advertised"), curr_raw.get("advertised")
    if prev_ua is not None and curr_ua is not None and prev_ua != curr_ua:
        if curr_ua:
            drafts.append(EventDraft(
                "ua_start", f"{app_name} started running ads (UA push)", {}
            ))
        else:
            drafts.append(EventDraft(
                "ua_stop", f"{app_name} stopped running ads", {}
            ))

    prev_countries = prev_raw.get("countries_list") or []
    curr_countries = curr_raw.get("countries_list") or []
    if prev_countries and curr_countries:
        prev_set = {c.upper() for c in prev_countries}
        curr_set = {c.upper() for c in curr_countries}
        added_c = sorted(curr_set - prev_set)
        removed_c = sorted(prev_set - curr_set)
        if added_c and looks_like_soft_launch(prev_countries) and (set(added_c) & GLOBAL_MARKERS):
            drafts.append(EventDraft(
                "global_launch",
                f"{app_name} went GLOBAL — expanded from {len(prev_set)} soft-launch "
                f"markets to {len(curr_set)} (added {', '.join(added_c[:10])})",
                {"added": added_c, "was_soft_launch_in": sorted(prev_set)},
            ))
        elif added_c:
            drafts.append(EventDraft(
                "countries_expanded",
                f"{app_name} expanded to {len(added_c)} new storefront(s): "
                f"{', '.join(added_c[:10])}{'…' if len(added_c) > 10 else ''}",
                {"added": added_c, "total_now": len(curr_set)},
            ))
        if removed_c:
            drafts.append(EventDraft(
                "countries_reduced",
                f"{app_name} pulled out of {len(removed_c)} storefront(s): "
                f"{', '.join(removed_c[:10])}{'…' if len(removed_c) > 10 else ''}",
                {"removed": removed_c, "total_now": len(curr_set)},
            ))

    return drafts


def analyze_rank_moves(
    prev_ranks: dict[str, int],
    curr_ranks: dict[str, int],
    chart_label: str,
    names: Optional[dict[str, str]] = None,
    entry_top: Optional[int] = None,
    jump_min: Optional[int] = None,
) -> list[tuple[str, EventDraft]]:
    """Compare two days of one chart (app_id -> rank maps).

    Emits, per app: 'chart_entry' (newly inside the top ``entry_top``),
    'rank_jump' (gained >= ``jump_min`` positions), and 'chart_leader_change'
    (a new #1). Returns (store_app_id, draft) pairs.
    """
    entry_top = entry_top if entry_top is not None else settings.rank_entry_top
    jump_min = jump_min if jump_min is not None else settings.rank_jump_min
    names = names or {}
    if not prev_ranks or not curr_ranks:
        return []

    results: list[tuple[str, EventDraft]] = []

    def label(app_id: str) -> str:
        return names.get(app_id, app_id)

    prev_leader = min(prev_ranks, key=prev_ranks.get) if prev_ranks else None
    curr_leader = min(curr_ranks, key=curr_ranks.get) if curr_ranks else None
    if (curr_leader and prev_leader and curr_leader != prev_leader
            and curr_ranks[curr_leader] == 1 and prev_ranks[prev_leader] == 1):
        results.append((curr_leader, EventDraft(
            "chart_leader_change",
            f"New #1 on {chart_label}: {label(curr_leader)} "
            f"(displaced {label(prev_leader)})",
            {"new_leader": curr_leader, "old_leader": prev_leader},
        )))

    for app_id, rank in curr_ranks.items():
        prev = prev_ranks.get(app_id)
        if rank <= entry_top and (prev is None or prev > entry_top):
            came_from = f"#{prev}" if prev is not None else "outside the chart"
            results.append((app_id, EventDraft(
                "chart_entry",
                f"{label(app_id)} entered the top {entry_top} on {chart_label} "
                f"at #{rank} (from {came_from})",
                {"rank": rank, "previous_rank": prev, "entry_top": entry_top},
            )))
        elif prev is not None and (prev - rank) >= jump_min and rank <= entry_top * 2:
            # jumps only matter near the top of the chart — #180 -> #150 is noise
            results.append((app_id, EventDraft(
                "rank_jump",
                f"{label(app_id)} jumped {prev - rank} places on {chart_label}: "
                f"#{prev} -> #{rank}",
                {"rank": rank, "previous_rank": prev, "gained": prev - rank},
            )))

    return results
