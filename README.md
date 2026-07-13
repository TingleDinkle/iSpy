# iSpy — Game Market Intelligence Tracker

Automated App Store / Google Play market-intelligence desk built on the
[AppstoreSpy REST API](https://api.appstorespy.com/docs), tuned for game
devs. Daily it pulls metadata, revenue/downloads estimates, Play installs,
reviews, category chart rankings, and competitor-studio portfolios into
PostgreSQL — then detects what changed (updates shipped, soft launches, UA
pushes, creative swaps, chart moves, review-topic surges, revenue spikes)
and delivers a digest to Discord/Slack.

**→ Start with [GUIDE.md](GUIDE.md) — the complete usage guide.**

## Architecture

```
                 ┌───────────────────────────────────────────┐
                 │            AppstoreSpy API (v1)            │
                 │  API-KEY header · 100k credits/month plan  │
                 └──────────────────┬────────────────────────┘
                                    │ HTTPS
                 ┌──────────────────▼────────────────────────┐
                 │  tracker/api_client.py                     │
                 │  throttle · retries (429/5xx/network)      │
                 │  exp. backoff + full jitter · Retry-After  │
                 │  credit ledger + monthly budget guard      │
                 └──────────────────┬────────────────────────┘
                                    │
      cron / Task Scheduler  ┌──────▼─────────┐   ┌───────────────────┐
      (daily, after 00:00Z)  │ daily_snapshot │   │ detect_spikes.py  │
                             │      .py       │   │ pandas · 7-day MA │
                             └──────┬─────────┘   └────────▲──────────┘
                                    │ upserts (idempotent)  │ reads
                 ┌──────────────────▼───────────────────────┴────────┐
                 │                PostgreSQL (SQLAlchemy 2.0)         │
                 │ apps · app_snapshots · monthly_estimates ·        │
                 │ daily_installs · reviews · api_requests · alerts  │
                 └────────────────────────────────────────────────────┘
```

- **`tracker/`** — library code: `config.py` (pydantic-settings, `.env`),
  `db.py` (engine/session), `models.py` (schema), `api_client.py` (resilient
  HTTP), `ingest.py` (idempotent upserts).
- **`tests/`** — 96 unit tests (no network, no database): throttling, backoff
  (numeric *and* HTTP-date `Retry-After`), retry classification, credit-ledger
  accounting (including fail-closed behavior when DB writes fail), estimates
  batching, the reviews pagination cap, spike math, missing-day handling, and
  installs-feed cleaning. Run with `pip install -r requirements-dev.txt` then
  `python -m pytest tests`.
- **`manage.py`** — CLI: create schema, manage the tracked-app registry,
  inspect credit usage.
- **`daily_snapshot.py`** — the daily collector (schedule this).
- **`detect_spikes.py`** — the analysis job (schedule after the collector).

## Database schema

| Table | Grain | Purpose / key columns |
|---|---|---|
| `apps` | one per tracked app | registry: `store`, `store_app_id`, name, developer, `tier` (primary/watch), `is_active` |
| `app_snapshots` | app × day | daily sample of metadata + **rolling monthly** revenue/downloads estimates, rating, whatsnew/icon/screenshots/countries/advertised in `raw` JSONB |
| `monthly_estimates` | app × month | revenue/downloads history from `/estimates` |
| `daily_installs` | app × day (Play only) | `ipd` + lifetime counter, stored verbatim |
| `reviews` | one per review | stars, text, author, `created_at`, `topics` (game-design taxonomy), `raw` |
| `rankings` | chart slot × day | top-chart positions for watched category charts (untracked apps included) |
| `ranking_watches` | chart | which (store, country, category) charts to pull daily |
| `developers` / `developer_apps` | studio / portfolio app | the new-game radar baseline |
| `app_events` | one per event | the intelligence feed: version/creative/UA/launch/chart/review events, deduped by `dedupe_key` |
| `market_segments` / `market_snapshots` | segment / segment × date | saved market filters + weekly aggregate sizing |
| `alerts` | app × metric × day | revenue/installs spikes and rating drops |
| `api_requests` | one per HTTP call | the credit ledger |

Dashboard-ready SQL views (`v_metric_history`, `v_rank_history`,
`v_events_feed`, `v_review_topics_weekly`, …) are created by `init-db`;
`docker compose up -d db metabase` gives you PostgreSQL + Metabase.

All writes are `INSERT … ON CONFLICT` upserts, so every job is safe to re-run.

## Verified API facts (live-tested July 2026)

These were confirmed against the real API, not just the docs — several differ
from what you'd assume:

1. **Revenue is monthly, not daily.** `/{store}/estimates` returns one row per
   *month* (`{"month": "2026-06", "revenue": 50000000, ...}`), heavily rounded.
   There is no daily-revenue endpoint. The daily revenue series this project
   analyses is the **rolling monthly estimate sampled once per day** from the
   app-details endpoint — a genuine 50 % jump in that estimate is a strong
   market signal.
2. **True daily data exists only for Play installs** (`/play/apps/{id}/installs_daily`)
   — and the feed is dirty: zero-filled gaps, days where `ipd` equals the
   lifetime cumulative counter (backfill dumps of 2.2 B installs), and negative
   counter resets. `detect_spikes.py` masks all of these before computing
   baselines.
3. **iOS review sort is `-date`** (not `created`). **Play review date-sort
   returns HTTP 500** server-side, so Play reviews are fetched in default
   order; raise `REVIEWS_PAGE_SIZE` (max 1000) for wider coverage.
4. **Play reviews require `language`** from a fixed enum
   (`en_US`, `en_GB`, `de_DE`, `fr_FR`, `ja_JP`, …). Set `PLAY_REVIEW_LANGUAGE`.
5. **Transient 500s happen on valid requests** — the client retries them.
6. `202` = app queued for crawling, `204` = no data for country, `403` = bad
   key, `429` = rate limited.

## Setup

```bash
python -m venv .venv && .venv/Scripts/activate   # Windows
pip install -r requirements.txt

createdb appstore_tracker                        # or via pgAdmin
cp .env.example .env                             # add your API key + DB URL

python manage.py init-db
python manage.py add-app ios 553834731           # Candy Crush (iOS)
python manage.py add-app play com.king.candycrushsaga
```

## Daily operation

```bash
python daily_snapshot.py          # collect (≈ 2–3 credits per app per day)
python detect_spikes.py           # analyse revenue vs 7-day MA, write alerts
python detect_spikes.py --metric installs   # Play daily-install spikes
python manage.py credits          # budget check
```

Schedule both daily (collector first). Windows Task Scheduler:

```powershell
schtasks /Create /SC DAILY /ST 06:00 /TN "iSpy snapshot" `
  /TR "C:\path\to\.venv\Scripts\python.exe C:\path\to\iSpy\daily_snapshot.py"
schtasks /Create /SC DAILY /ST 06:30 /TN "iSpy spikes" `
  /TR "C:\path\to\.venv\Scripts\python.exe C:\path\to\iSpy\detect_spikes.py"
```

## Credit budgeting (100 000/month)

Per app per day: 1 details + 1 reviews + 1 installs (Play only), plus
~1 batched estimates call per 25 apps per store. Approximate monthly usage:

| Tracked apps | Credits/month |
|---|---|
| 100 (mixed) | ≈ 9 500 |
| 500 (mixed) | ≈ 47 000 |
| 1 000 (mixed) | ≈ 94 000 ← near the cap |

The client hard-stops at 95 % of budget (`CREDIT_SAFETY_FRACTION`), and
`manage.py credits` shows month-to-date burn. To stretch the budget, run
reviews weekly: `daily_snapshot.py --skip-reviews` on six days out of seven.

## Spike detection

`detect_spikes.py` re-indexes each app's series to calendar-daily frequency,
computes the mean of the **prior** `--window` days (default 7, current day
excluded, ≥ `--min-periods` data points required, default 4), and flags days
where `value ≥ baseline × 1.5` (default `--threshold 50`) with
`baseline ≥ --min-baseline` (default 1000 — suppresses noise from tiny apps).
Alerts are deduplicated per `(app, metric, day)`, so the cron job never
re-alerts on the same event.

For Play installs, artifact days are masked before any baseline is computed:
non-positive values, days whose `ipd` equals the lifetime counter (backfill
dumps), and days exceeding `INSTALLS_OUTLIER_MULTIPLIER` × the rolling median
(default 100× — far above genuine viral spikes at ~25–50×, far below dump
artifacts at 1000×+; a global-median fallback covers an app's first days,
where no rolling verdict exists yet).

## Security notes

- The API key lives only in `.env`, which is gitignored. Never commit it.
- **The key used during development was pasted in plain text into a chat
  session — rotate it in your AppstoreSpy dashboard if that concerns you.**

## Next steps (not included)

- Alembic migrations (schema is currently `create_all`-managed)
- Notification sink for alerts (email / Slack / Discord webhook)
- Per-endpoint credit weights if AppstoreSpy bills some calls > 1 credit
