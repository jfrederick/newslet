# Design: homepage freshness, "off your beat" articles, and a Discover page

Date: 2026-07-07
Status: approved

Three independent features, delivered as three serial PRs in this order so
each merges cleanly on top of the last.

## 1. Homepage freshness — cron-authoritative, never blocking, Eastern day

### Problem

The homepage regularly shows old articles plus a blocking
"Preparing today's edition…" spinner for minutes. Two causes:

1. **UTC day boundary.** `handlers/web.py:home()` marks the stored `home`
   edition stale when `issue.created_at.date() != now.date()` — both UTC.
   The daily rebuild cron fires at 09:45 UTC, so any visit after ~19:00
   Eastern (00:00 UTC) sees "yesterday's" build, reads as stale, and kicks
   a slow on-visit rebuild — every evening.
2. **Blocking on-visit rebuild.** The stale path fires `/api/home/refresh`
   (a full async digest run, ~1 min, sometimes longer or failing) and parks
   a full-screen spinner over the stale content while polling.

### Decision (user-approved)

Cron-only refresh; the page never blocks and never rebuilds on visit; the
"today" boundary is **America/New_York**.

### Changes

- **New `src/newslet/clock.py`** — owns the day boundary:
  `EASTERN = ZoneInfo("America/New_York")`, `local_date(dt)` (convert an
  aware datetime to the Eastern calendar date), `today(now=None)`. No other
  module hardcodes a timezone.
- **`handlers/web.py:home()`** — staleness becomes
  `clock.local_date(issue.created_at) != clock.local_date(now)`; the
  `date_header` renders the Eastern date.
- **`templates/read.html.j2`** — delete the `autoRefresh()` kickoff, the
  polling, and the full-screen "Preparing today's edition…" spinner. The
  page always renders the latest stored edition immediately. When that
  edition is not from today (Eastern), render a small non-blocking notice
  ("Showing {weekday}'s edition — today's update hasn't run yet.") above
  the cards.
- **Kept:** the 09:45 UTC `HomeRefreshSchedule` cron (≈04:45–05:45 ET — the
  correct Eastern morning; no schedule change), `_run_home`, and the
  `/api/home/refresh` + `/api/home/status` endpoints (still used by ops /
  future manual controls; no longer wired to page load).

### Testing

- `test_clock.py`: Eastern date conversion incl. the evening-UTC-rollover
  case that caused the bug.
- `test_web.py`: a home edition built this morning Eastern but "yesterday"
  in UTC renders **without** the stale notice; a genuinely old edition
  renders content **with** the notice and no refresh kick.

## 2. "Off your beat" — a serendipity article block

### Decision (user-approved)

A distinct block (not folded into ranked picks) on **both** the homepage
and the daily email: popular articles from the past week that the reader
might enjoy, using the profile for human taste (hobbies, non-work
interests) but **hard-excluding computers / software / programming / AI /
ML / tech-industry topics**. An admin setting controls the count.

### Changes

- **New `src/newslet/serendipity.py`** — mirrors `websearch.py`:
  `fetch_serendipity(profile_md, *, max_results, client=None) ->
  list[WebArticle]`. Claude `web_search`, past ~7 days, popular /
  widely-shared, profile-informed but tech-excluded. Best-effort: `[]` on
  any failure; client injectable.
- **`contracts.py`** — `Issue.random_articles: list[WebArticle] = []`;
  `Config.max_random_articles: int = Field(default=4, ge=0, le=20)`
  (0 disables, like `max_web_articles`).
- **`db.py`** — persist/read `random_articles_json` leniently, exactly like
  `web_articles_json`.
- **`handlers/digest.py`** — `run_digest` gains `serendipity_fn` +
  `max_random`; block built best-effort and seen-filtered; the email path
  marks its urls seen after a confirmed send. `_fresh_issue` and
  `_run_home` pass `config.max_random_articles` (the homepage may use a
  slightly higher floor like the other home counts).
- **Surfaces** — an "Off your beat" section on the homepage (votable cards,
  same `/api/vote` flow) and in the email (votable via signed `/rate`
  links, rendered distinctly from picks).
- **Admin** — a "random articles" count field beside the web/X counts.

### Testing

`test_serendipity.py` (fake client: parse, cap, dedupe, tech-exclusion
prompt content, failure → empty); digest wiring (block present, failure
degrades, seen-filtering); render tests for both surfaces; config
round-trip.

## 3. Discover page — recommended RSS feeds and X accounts

### Decision (user-approved)

A new admin-authed `/discover` page listing (a) RSS feeds and (b) X
accounts the user might like. Recommendations are **precomputed by a
scheduled run and stored** (page loads instantly); a manual
"Refresh recommendations" button re-generates without blocking. Feed cards
get a one-click "Add to my feeds"; X accounts get a "View on X" link.

### Changes

- **`contracts.py`** — `DiscoverFeed` (title, site_url, feed_url, reason),
  `DiscoverAccount` (handle, name, reason, url), `DiscoverBoard`
  (feeds, accounts, generated_at).
- **New `src/newslet/discover.py`** (source-level; distinct from the
  article-level `discovery.py`) —
  `build_discover_board(profile_md, followed_domains, *, client=None)`.
  Feeds via Claude `web_search`, excluding already-followed domains, each
  `feed_url` liveness-checked (reuse the `_feed_is_live` logic — move it to
  `search_common.py` so `discovery.py` and `discover.py` share it).
  X accounts via Claude `web_search` (no XAI key needed), with light
  `x.com/<handle>` shape validation.
- **`db.py`** — `get_discover()` / `put_discover()` storing the board JSON
  in the profile table under `id="discover"` (mirrors `get_config`).
  Lenient read: missing/bad row → empty board.
- **`handlers/digest.py`** — `{"discover": true}` event mode →
  `_run_discover`, building and storing the board.
- **`infra/template.yaml`** — weekly `DiscoverSchedule` on the digest
  Lambda: `cron(30 9 ? * MON *)`.
- **`handlers/web.py`** — `GET /discover` (renders the stored board;
  already-followed feeds hidden), `POST /api/discover/refresh`
  (async-invoke, same pattern as home refresh), `GET /api/discover/status`
  (generated_at polling for the inline "refreshing…" affordance).
  Feed add reuses `POST /api/feeds`.
- **`templates/discover.html.j2`** + a "discover" nav link on the
  homepage/admin/emails headers.

### Testing

`test_discover.py` (fake client + fake feed validator: parse, exclusion,
liveness filtering, failure → empty board); db round-trip; web tests for
page render, hidden already-followed feeds, refresh invoke, status; digest
mode routing.

## Cross-cutting

- All new network edges are injectable; the offline test suite stays
  offline.
- Best-effort convention holds: no new source or block may break a send or
  a page render.
- `AGENTS.md` architecture map, `DESIGN.md`, and `docs/product.md` are
  updated in the PR that changes what they describe.
- Delivery: three serial PRs — freshness, serendipity, discover.
