# iSpy — User Guide for Game Devs

Your personal market-intelligence desk for the App Store and Google Play,
built on the AppstoreSpy API (100k credits/month plan). It watches your
genre's charts, your competitors' updates and launches, their reviews, and
the size of your market — and delivers what changed to Discord every morning.

Everything below was tested end-to-end against the live API on 2026-07-13.

---

## 1. What you get

| Question you ask as a game dev | Where the answer lives |
|---|---|
| "Who's climbing the puzzle charts?" | `rankings` table, chart events in the digest |
| "What did Royal Match just ship?" | `version_update` events with the full patch notes |
| "Is a competitor soft-launching something?" | `new_developer_app` / `soft_launch_detected` events |
| "Did someone start a UA push or change store creatives?" | `ua_start` / `icon_change` / `screenshots_change` events |
| "Are players revolting about a mechanic?" | review topics (`difficulty`, `monetization`, …), `review_topic_surge` events, rating-drop alerts |
| "Is my genre growing?" | `market_report.py` — apps live, downloads/mo, revenue/mo per saved segment |
| "Whose revenue spiked 50%?" | `detect_spikes.py` alerts vs 7-day moving average |

Your setup is already seeded with: 16 puzzle/casual genre leaders (Candy
Crush, Royal Match, Gardenscapes, Homescapes, Merge Mansion, Gossip Harbor,
Block Blast, Coin Master — both stores), 14 studio watches (King, Dream
Games, Playrix, Metacore, Microfun, HungryStudio, Moon Active…), 3 chart
watches (iOS Puzzle, Play Puzzle, Play Casual — US, top 200), and 3 market
segments. Day-1 data is already in the database.

## 2. One-time setup

```bash
pip install -r requirements.txt
docker compose up -d db metabase      # Postgres (host port 5433) + dashboard
```

Your `.env` needs (the first two are already set):

```
APPSTORESPY_API_KEY=...
DATABASE_URL=postgresql+psycopg://tracker:tracker@localhost:5433/appstore_tracker
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...   # see §6
```

The schema and seed data already exist. On a brand-new machine it would be:
`python manage.py init-db` then `python manage.py seed --yes`.

## 3. The daily rhythm

Run these in order once a day (or schedule them, §7):

```bash
python daily_snapshot.py     # pull everything (~60 credits at current scale)
python detect_events.py     # diff snapshots + charts -> events   (0 credits)
python analyze_reviews.py   # tag reviews, rating drops, surges   (0 credits)
python detect_spikes.py     # revenue vs 7-day moving average     (0 credits)
python notify.py            # deliver the digest to Discord       (0 credits)
```

Weekly (Mondays are nice):

```bash
python market_report.py     # segment sizing, 1 credit per segment
python detect_spikes.py --metric installs   # Play install spikes
```

What to expect on the timeline:
- **Day 1**: metrics, reviews, charts land. No events yet (diffs need 2 days).
- **Day 2+**: chart moves, update/creative/UA events start flowing.
- **Day ~5**: revenue spike detection has enough baseline (needs 4+ days).
- **Week 2**: review topic surges and rating-drop alerts become meaningful.

## 4. Managing what you track

```bash
python manage.py list-apps                       # what's tracked now
python manage.py add-app ios 1482155847          # add by store id (1 credit)
python manage.py add-app play com.some.game --tier watch
python manage.py set-tier play com.some.game primary
python manage.py deactivate-app ios 123456       # stop pulling, keep history

python manage.py add-developer play 6577204690045492686 --name King
python manage.py list-developers                 # studio watch list

python manage.py add-watch ios US GAMES_STRATEGY # another genre chart
python manage.py add-watch play US GAME_STRATEGY --top 100
python manage.py list-watches

python manage.py add-segment ios-strategy-1m ios --category GAMES_STRATEGY --min-revenue 1000000
python manage.py list-segments

python manage.py credits                         # month-to-date burn
```

**Tiers.** `primary` apps get the full daily pull (details + reviews + Play
installs). `watch` apps refresh only weekly and skip reviews/installs — ~90%
cheaper. Keep direct competitors primary; genre curiosities watch. New games
found by the studio radar are auto-tracked as `watch`.

**Category names differ per store**: iOS `GAMES_PUZZLE`, `GAMES_STRATEGY`,
`GAMES_WORD`…; Play `GAME_PUZZLE`, `GAME_CASUAL`, `GAME_STRATEGY`… (note
iOS has no Casual category — Candy Crush lives in `GAME_CASUAL` on Play and
`GAMES_PUZZLE` on iOS).

**Finding a store id**: it's in the app's store URL —
`apps.apple.com/app/id1482155847` → `1482155847`;
`play.google.com/store/apps/details?id=com.dreamgames.royalmatch` → the id.

## 5. Reading the intelligence

**The digest** (Discord, or `python notify.py --dry-run` in a terminal) is
ordered by signal: updates shipped → launch radar → UA & creative → review
signals → chart moves. Untracked chart apps appear as clickable store links.

**Events worth acting on:**
- `version_update` — includes the competitor's patch notes verbatim. Watch
  what they shipped, then check their rank/revenue 7 days later.
- `soft_launch_detected` — a watched studio put a game live in test markets
  only (PH/CA/AU/NZ-style, no US/GB). Months of lead time on their launch.
- `global_launch` — a soft-launched title just added US/GB. It graduated.
- `ua_start` — they turned on ads; expect chart movement within days.
- `review_topic_surge` — e.g. crash complaints tripled this week; includes 3
  sample quotes. Works on competitors too: their difficulty-spike complaints
  are your design lesson for free.
- `chart_entry` into the top 50 — new blood in your genre; the link opens
  the store page.

**Review topics** are keyword rules in [tracker/review_topics.py](tracker/review_topics.py)
(`crash_bug`, `monetization`, `ads_complaints`, `difficulty`,
`progression_grind`, `content_drought`, `multiplayer_matchmaking`,
`controls_ux`, `account_data_loss`, `praise`). Edit them freely, then:
`python analyze_reviews.py --retag`. Weekly breakdown per app:
`python analyze_reviews.py --report`.

**Play review caveat**: the API can't sort Play reviews by date and caps at
1000 per pull, so Play review coverage is a helpfulness-weighted sample, not
a census. iOS reviews are newest-first and complete for practical purposes.

**Revenue numbers are rolling monthly estimates**, heavily rounded upstream
(Candy Crush = "$50M"). A 50% move in that number is a real market event; a
5% wiggle is rounding. True daily granularity exists only for Play installs.

## 6. Discord setup (2 minutes)

1. Discord → your server → **Server Settings → Integrations → Webhooks →
   New Webhook**, pick a channel like `#market-intel`, **Copy Webhook URL**.
2. Paste into `.env`: `DISCORD_WEBHOOK_URL=...`
3. Test: `python notify.py` (anything unsent goes out; `--dry-run` previews).

Undelivered items stay queued — if the webhook is down or unset, nothing is
lost; the next successful `notify.py` run sends the backlog.
`SLACK_WEBHOOK_URL` works the same way (both can be set at once).

## 7. Scheduling (Windows)

```powershell
$py = "C:\path\to\python.exe"; $repo = "C:\Users\Omen\Downloads\iSpy"
schtasks /Create /SC DAILY /ST 06:00 /TN "iSpy collect" /TR "cmd /c cd /d $repo && $py daily_snapshot.py && $py detect_events.py && $py analyze_reviews.py && $py detect_spikes.py && $py notify.py"
schtasks /Create /SC WEEKLY /D MON /ST 06:45 /TN "iSpy market" /TR "cmd /c cd /d $repo && $py market_report.py && $py notify.py"
```

(`.env` is found relative to the repo regardless of the working directory,
but `cd /d` keeps relative paths tidy.) On Linux/macOS the equivalent cron:

```cron
0 6 * * *  cd /path/to/iSpy && python daily_snapshot.py && python detect_events.py && python analyze_reviews.py && python detect_spikes.py && python notify.py
45 6 * * 1 cd /path/to/iSpy && python market_report.py && python notify.py
```

## 8. Dashboard (Metabase)

`docker compose up -d metabase` → http://localhost:3000 → create an admin
account → **Add database**: PostgreSQL, host `db`, port `5432`, database
`appstore_tracker`, user/password `tracker`/`tracker`.

Pre-built views to chart directly (no SQL needed):

| View | Chart it as |
|---|---|
| `v_metric_history` | revenue/downloads/rating per app over time |
| `v_rank_history` | chart position over time (invert the Y axis!) |
| `v_events_feed` | the intelligence feed as a table |
| `v_review_topics_weekly` | stacked area of complaint topics per app |
| `v_rating_weekly` | star trend per app |
| `v_market_history` | your genre's size over time |
| `v_latest_metrics` | current-state leaderboard of tracked apps |
| `v_alerts_feed` | spike/rating alerts |

## 9. Credit budget (100k/month)

Measured live: initial seed + first full day + market report ≈ **146
credits**. Steady-state daily at current scale:

| Stage | Credits/day |
|---|---|
| 16 primary apps (details + reviews + Play installs) | ~40 |
| Estimates (batched) | 2 |
| 3 chart watches (top 200, paginated) | ~9 |
| 14 studio portfolio queries | ~14 |
| **Total** | **~65/day ≈ 2k/month (2% of plan)** |

You have enormous headroom: every +10 primary apps ≈ +25/day; every new
chart watch ≈ +3/day; watch-tier apps ≈ 0.3/day each. The client hard-stops
at 95% of budget (`manage.py credits` to check), so a runaway can't eat the
plan. If you ever approach the cap: move apps to `watch`, run
`daily_snapshot.py --skip-reviews` some weekdays, or trim `--top` on watches.

## 10. Tuning knobs (.env)

| Knob | Default | Meaning |
|---|---|---|
| `SPIKE_THRESHOLD_PCT` | 50 | revenue/installs spike sensitivity |
| `RANK_ENTRY_TOP` | 50 | "entered top N" event range |
| `RANK_JUMP_MIN` | 20 | positions gained to report a jump |
| `RATING_DROP_STARS` | 0.5 | 7d star-average drop that alerts |
| `TOPIC_SURGE_MIN` / `TOPIC_SURGE_RATIO` | 5 / 3.0 | review-topic surge sensitivity |
| `SOFT_LAUNCH_MAX_COUNTRIES` | 15 | storefront count for soft-launch heuristic |
| `WATCH_REFRESH_DAYS` | 7 | watch-tier refresh cadence |
| `PLAY_REVIEW_LANGUAGE` | en_US | Play review language (enum) |

## 11. Troubleshooting

- **"No data yet (queued for crawl)"** — the app wasn't in AppstoreSpy's DB;
  it's queued (HTTP 202). Data appears within a day or two automatically.
- **Rankings look 2 days behind** — the upstream chart feed lags ~2 days.
  The puller re-fetches a 3-day window daily, so nothing is missed.
- **Zero events on day 1** — correct; diffs need two days of snapshots.
- **A digest didn't arrive** — check `python notify.py -v`; failed sends stay
  queued and retry on the next run. `--dry-run` previews without sending.
- **Transient 500s in logs** — normal for this API; the client retries with
  backoff. Only repeated `RetriesExhausted` for the same endpoint matters.
- **LiveOps endpoint** — wired up (`client.get_liveops`) but the API returns
  500 server-side as of July 2026. Nag AppstoreSpy support; when they fix
  it, competitor event calendars become available.
- **Schema changes later** — Alembic is configured: `alembic stamp head`
  once on the existing DB, then `alembic revision --autogenerate` +
  `alembic upgrade head` per change.

## 12. Development

```bash
pip install -r requirements-dev.txt
python -m pytest tests            # 164 tests, no network/DB needed
```

CI runs the suite on every push (`.github/workflows/ci.yml`). The API key
lives only in `.env` (gitignored) — and since yours was once pasted into a
chat session, rotating it in the AppstoreSpy dashboard is cheap insurance.
