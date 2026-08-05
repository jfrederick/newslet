# AGENTS.md

Operational guide for AI agents (and humans) working in this repo. Keep it
lean — it's loaded into context every session, so prefer pointers over prose.

> **Documentation scaffolding:** for every PR, consider whether this file,
> `CLAUDE.md`, `README.md`, `DESIGN.md`, or any linked sub-file needs an
> update to stay accurate, and update it in the same PR.

## What this is

`newslet` is a personal daily RSS newsletter. A scheduled Lambda fetches the
last 24h from your feeds, asks Claude to rank/summarize them against a
profile, surfaces a few "discovery" sources you don't follow yet, and emails
the result via Resend. `+`/`−` rate links in the email are HMAC-signed and
handled by a second (web) Lambda backed by DynamoDB.

- **`README.md`** — how to deploy and operate it.
- **`DESIGN.md`** — the interface contract every module follows. Read it
  before changing a module boundary.

## Environment

- **Python 3.12+ is required** (`requires-python = ">=3.12"`). The default
  `python3` on some machines is 3.11 and will fail `pip install -e .` with a
  version error — create the venv explicitly with 3.12:

  ```bash
  python3.12 -m venv .venv
  .venv/bin/pip install -e ".[dev]"
  ```

## Build / test / lint

Both of these are CI gates (`.github/workflows/ci.yml`). Run **both** before
pushing — running only `pytest` will miss lint failures:

```bash
.venv/bin/ruff check src tests scripts
.venv/bin/python -m pytest -q
```

Render a sample email locally (no network, no AWS) to eyeball template
changes:

```bash
.venv/bin/python scripts/dry_run.py && open out/email.html
```

Both preview scripts take an optional theme name (e.g.
`scripts/dry_run.py phosphor`) to eyeball a non-default theme.

Render the homepage locally (moto-backed, no network) — the latest email
rendered with the web nav strip:

```bash
.venv/bin/python scripts/preview_read.py && open out/read.html
```

## Architecture map

| Module | Responsibility |
| --- | --- |
| `clock.py` | the app's calendar-day boundary (US Eastern): `local_date`/`local_now`; nothing else hardcodes a timezone |
| `config.py` | `Settings` — env vars + SSM SecureString lookups for secrets |
| `contracts.py` | pydantic models at every JSON/DB boundary (Article, Pick, Issue, Discovery, WebArticle, …) |
| `tokens.py` | HMAC sign/verify for `/rate` links |
| `feeds.py` | feedparser wrapper, 24h filter, dedup via injected `is_seen` |
| `hn.py` | Hacker News via the Algolia API (rich content), injected `fetch`; feeds the ranking pool + the web view |
| `search_common.py` | shared Claude `web_search` primitives (tool def, last-text-block, JSON extraction, host key) used by `discovery` + `websearch` |
| `websearch.py` | Claude `web_search` for the "from around the web" block |
| `facts.py` | two ~500-word tech-fact essays per issue (8-genre methodology, 60-topic no-repeat log) + the facts-only tuner; state on the `id="facts"` profile row |
| `quotes.py` | the philosophical quote of the day (epigraph): real attributable quotes, tradition rotation, 120-entry no-repeat log + quotes-only tuner; state on the `id="quotes"` profile row |
| `weather.py` | one terse Brooklyn forecast line via the free NWS API (no LLM); stamped on `Issue.weather_line` |
| `serendipity.py` | Claude `web_search` for the "off your beat" block: popular past-week articles outside the reader's tech beat (profile for human taste only; computers/AI hard-excluded) |
| `x_grok.py` | X (Twitter) ranking candidates via xAI Grok `x_search` tool (Responses API), injected `complete`; on only when `XAI_API_KEY` is set |
| `newsletters.py` | parse inbound newsletter email → `Article` candidates; double-opt-in detection; address minting (pure, no DB/network) |
| `discover.py` | Claude `web_search` for the Discover page's stored board: RSS feeds + X accounts matched to the profile (source-level; distinct from the article-level `discovery.py`) |
| `db.py` | boto3 DynamoDB wrappers (7 tables) |
| `rank.py` | Anthropic ranking call with prompt caching |
| `discovery.py` | Claude web-search for sources outside your feeds |
| `summarize.py` / `tune.py` | subject/intro writing; profile auto-tuning |
| `email_render.py` | Jinja → `(subject, html)` (configurable counts; HN + web block; generic homepage link; theme-aware inline styles; `web_nav=True` adds the homepage's nav strip) |
| `themes.py` | named visual themes (color/font/radius tokens) for web + email — the Claude-chat family (Foundry default, Atelier, Manuscript, Observatory, Meadow), Classic, and the textmode set; `get()` is lenient, `css()` emits the `:root` vars (incl. the text-size root `font-size`) the web templates style against |
| `handlers/digest.py` | scheduled Lambda + dry-run CLI; `{"manual"}` send-now and `{"discover"}` discover-board modes |
| `handlers/inbound.py` | SES-invoked Lambda: parse received newsletter mail → store links / auto-confirm opt-ins (S3 read + confirm-follow injectable) |
| `handlers/web.py` | FastAPI + Mangum (`/` homepage = latest email + nav, `/discover`, `/admin`, `/docs` product guide, `/emails` + `/emails/{date}` archive, `/rate`, `/api/hn`, `/api/config`, `/api/subscriptions`, `/api/discover/*`) |
| `docs/product.md` + `docs/index.html` | the **product guide**: canonical markdown + a self-contained HTML viewer that fetches it live (3 selectable detail levels). Served at `/docs`; the markdown is the single source of truth |
| `templates/email.html.j2` | the email body; also the homepage body (rendered with `web_nav=True`) |
| `templates/emails.html.j2` | the sent-email archive list |
| `templates/discover.html.j2` | the Discover page: stored feed + X-account recommendations, one-click feed add, non-blocking refresh |
| `templates/admin.html.j2` | admin UI (feeds, profile, daily-email settings, theme picker, send now) |
| `infra/template.yaml` | SAM stack |

## Conventions and invariants

- **Signed email links:** `tokens.sign(article_url, issue_date)`. The issue date is
  part of every signed message and bounds replay scope. Mirror the existing
  `/rate` pattern for any new email-clickable action.
- **Best-effort enrichment:** summarize, discovery, the Hacker News source
  (`hn.fetch_hn_articles`), the web-search block (`websearch.search_web`), the
  off-your-beat block (`serendipity.fetch_serendipity`), the tech-fact
  blocks (`facts.fetch_facts`), the quote of the day (`quotes.fetch_quote`),
  the weather line (`weather.fetch_weather`), the
  subscribed-newsletter source (`db.recent_inbox_articles`), and the X source
  (`x_grok.fetch_x_articles`) must never block a send — they degrade to empty
  on any failure. Keep new enrichment steps in the same `try/except → empty`
  shape, and make their network edge injectable (HN takes a `fetch` callable;
  websearch/serendipity a `client`; X a `complete` callable; `run_digest`
  takes `hn_fn`/`websearch_fn`/`newsletters_fn`/`x_fn`/`serendipity_fn`) so
  tests stay offline.
- **Optional-source keys:** sources gated on a key the user may not have set
  (the X source's `XAI_API_KEY`) read it via `config._optional_secret`, which
  returns `""` (feature disabled) instead of raising, and only consults SSM
  inside Lambda — so a missing optional key never breaks `settings()` locally
  or in the offline test suite.
- **Newsletter source (inbound email):** SES receives mail on `MAIL_DOMAIN`,
  writes raw MIME to the inbox S3 bucket, and invokes `handlers/inbound.py`. It
  matches the recipient to a `Subscription` (per-source generated addresses),
  **auto-follows double-opt-in confirmation links**, and stores extracted
  article links in the inbox table for the digest to fold into its ranking
  pool. The handler **never raises** (a raise makes SES retry-storm); its S3
  read and confirm-follow are injectable so tests stay offline. The SES
  *receipt rule* resources are conditional on `MailDomain` being set — the rest
  of the infra (bucket, tables, Lambda) deploys regardless, and the active
  rule set must be set manually post-deploy (see `README.md`).
- **The homepage is the email:** the daily email (`Config.max_rss_articles`
  ranked picks from RSS + Hacker News + newsletters + optional X, plus
  `Config.max_web_articles` open-web results, `Config.max_random_articles`
  "off your beat" articles, and discoveries — all votable via the signed
  `/rate` links) is the one built surface. `/` re-renders the **newest
  delivered issue** (newest `sent_at` row; falls back to the newest stored
  row only when nothing in the last 60 editions was ever sent — a fresh
  install) through `email_render.render_email(web_nav=True)` (a thin
  nav strip on top; no rebuild, no LLM calls, no staleness logic — before
  today's send it shows yesterday's, clearly dated). `/emails/{date}`
  renders any archived issue **as-sent** (no nav strip); `/emails` is the
  index. There is no separate homepage aggregation anymore: the old
  `{"home"}` digest mode, its 09:45 UTC cron, `read.html.j2`, `/api/vote`,
  `/api/search`, and `/api/home/*` were all removed — a stray
  `{"home": true}` Lambda event falls through to the idempotent daily path.
- **Admin config** lives in the profile table under `id="config"`
  (`db.get_config`/`put_config`, model `contracts.Config`): `max_rss_articles`,
  `max_web_articles`, `max_random_articles` (the "off your beat" block's
  count; 0 disables it), `web_variety` (0–100 exploration dial for
  `websearch.search_web`), `x_enabled` (X source on/off; also needs
  `XAI_API_KEY`), `max_x_articles` (X posts pulled into the pool),
  `facts_enabled` (the two tech-fact essays; their votes tune the separate
  `id="facts"` profile, never the general one), `quote_enabled` (the
  philosophical epigraph; same separation via `id="quotes"`),
  `weather_enabled` (the NWS forecast line), `theme`
  (visual theme for the web pages *and* the daily email; resolve names via
  `themes.get`, which falls back to the default, Foundry, on unknown values),
  and `text_size` (75–150% dial; web pages scale via the root `font-size` —
  templates declare type in `rem` — and the email via scaled inline px). Read
  leniently (defaults on a missing/bad row; the X source has no per-account
  config — relevance comes from the profile, like HN/web). Issues stamp the
  appearance (`Issue.theme`/`text_size`) they were sent with so the
  `/emails/{date}` archive stays as-sent; legacy issue rows default to
  classic at 100% (historical accuracy), while a missing *config* defaults
  to Foundry.
- **Ranking is grounded to the candidate pool:** `rank.rank` keeps only picks
  whose URL was one of the candidates it was handed, dropping any the model
  invents. This is a freshness guard, not just hygiene — an ungrounded pick
  (a plausible story the model recalls from training) would bypass every
  upstream recency filter (the 24h RSS window, HN's recency cap) and surface
  stale content in the email (and therefore on the homepage). Preserve this
  when changing the rank output path.
- **Lenient on read, strict on write:** DB readers (`list_feeds`,
  `recent_feedback`, `get_issue`) skip-and-log bad/legacy rows rather than
  raising, so one bad row can't break a whole page. When you make a model
  field required, check the persisted-data read paths for older rows.
- **Discover page (`/discover`):** renders a *stored* board of recommended
  RSS feeds + X accounts (`db.get_discover`/`put_discover`, profile table
  `id="discover"`, model `contracts.DiscoverBoard`) — it never generates on
  visit. A weekly EventBridge rule (Mondays 09:30 UTC, `{"discover": true}`)
  rebuilds it via `discover.build_discover_board` (feed urls
  liveness-checked with `search_common.feed_is_live`; already-followed
  domains excluded at build, already-followed feeds also hidden at render);
  the page's "Refresh recommendations" button async-invokes the same event
  and polls `/api/discover/status`, non-blocking. A failed build keeps the
  previous board instead of overwriting it.
- **Manual "send now":** stores under a synthetic `manual-<ts>-<rand>` key
  that's hidden from "recent issues" and stays out of the daily cadence — see
  `digest._run_manual`. Don't surface that internal key in user-facing output.
- **Product guide (`src/newslet/docs/`):** `product.md` is the single source of
  truth; `index.html` fetches it at runtime and renders it client-side, so the
  two never drift (no md→html sync step needed). It lives under `src/` (not the
  top-level `docs/`) so it ships in the Lambda bundle and can be served at
  `/docs`. Complexity tiers are encoded as `:::tier little` / `:::tier medium`
  fences the viewer filters on; keep them balanced. A scheduled GitHub Actions
  step regenerates `product.md` from the code on pushes to `main`
  (`docs/docs-autoupdate-setup.md`), so keep the guide fact-based and let the
  code be the source of truth.
- **Match surrounding style:** comment density, naming, and idioms vary by
  file; follow the file you're editing.

## Testing patterns

- No test hits the network or real AWS. External edges are always stubbed:
  **Anthropic → `FakeClient`**, **AWS DynamoDB → moto**, **feedparser /
  resend → monkeypatched**.
- `test_integration.py` and `test_web.py` are integration-level (real modules
  composed, only the edges faked); the rest are unit tests.
- When adding a real network/IO call in production code, make it **injectable**
  (a callable arg, like `feeds.fetch_recent`'s `is_seen`) so tests can
  substitute a fake and stay offline.

## Git / PR workflow

- Develop on a feature branch; push with `git push -u origin <branch>`.
- Opening a PR for your changes is fine.
- CI runs `ruff` + `pytest` on every PR and, on merge to `main`, deploys via
  SAM. Don't push work that fails either gate.
