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

Render the rich issue web view locally (moto-backed, no network) to eyeball
`read.html.j2`:

```bash
.venv/bin/python scripts/preview_read.py && open out/read.html
```

## Architecture map

| Module | Responsibility |
| --- | --- |
| `config.py` | `Settings` — env vars + SSM SecureString lookups for secrets |
| `contracts.py` | pydantic models at every JSON/DB boundary (Article, Pick, Issue, Discovery, WebArticle, …) |
| `tokens.py` | HMAC sign/verify for `/rate` links |
| `feeds.py` | feedparser wrapper, 24h filter, dedup via injected `is_seen` |
| `hn.py` | Hacker News via the Algolia API (rich content), injected `fetch`; feeds the ranking pool + the web view |
| `websearch.py` | Claude `web_search` for the "from around the web" block + the web view's subject search |
| `newsletters.py` | parse inbound newsletter email → `Article` candidates; double-opt-in detection; address minting (pure, no DB/network) |
| `db.py` | boto3 DynamoDB wrappers (7 tables) |
| `rank.py` | Anthropic ranking call with prompt caching |
| `discovery.py` | Claude web-search for sources outside your feeds |
| `summarize.py` / `tune.py` | subject/intro writing; profile auto-tuning |
| `email_render.py` | Jinja → `(subject, html)` (configurable counts; HN + web block; generic homepage link) |
| `handlers/digest.py` | scheduled Lambda + dry-run CLI; `{"manual"}` send-now and `{"home"}` homepage-rebuild modes |
| `handlers/inbound.py` | SES-invoked Lambda: parse received newsletter mail → store links / auto-confirm opt-ins (S3 read + confirm-follow injectable) |
| `handlers/web.py` | FastAPI + Mangum (`/` homepage, `/admin`, `/docs` product guide, `/emails` + `/emails/{date}` archive, `/rate`, `/api/vote`, `/api/search`, `/api/hn`, `/api/config`, `/api/subscriptions`, `/api/home/*`) |
| `docs/product.md` + `docs/index.html` | the **product guide**: canonical markdown + a self-contained HTML viewer that fetches it live (3 selectable detail levels). Served at `/docs`; the markdown is the single source of truth |
| `templates/read.html.j2` | the homepage: rich reading UX (voting, subject search; auto-regenerates when stale) |
| `templates/emails.html.j2` | the sent-email archive list |
| `templates/admin.html.j2` | admin UI (feeds, profile, daily-email settings, send now) |
| `infra/template.yaml` | SAM stack |

## Conventions and invariants

- **Signed email links:** `tokens.sign(article_url, issue_date)`. The issue date is
  part of every signed message and bounds replay scope. Mirror the existing
  `/rate` pattern for any new email-clickable action.
- **Best-effort enrichment:** summarize, discovery, the Hacker News source
  (`hn.fetch_hn_articles`), the web-search block (`websearch.search_web`), and
  the subscribed-newsletter source (`db.recent_inbox_articles`) must never
  block a send — they degrade to empty on any failure. Keep new enrichment
  steps in the same `try/except → empty` shape, and make their network edge
  injectable (HN takes a `fetch` callable; websearch a `client`; `run_digest`
  takes `hn_fn`/`websearch_fn`/`newsletters_fn`) so tests stay offline.
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
- **Email vs. homepage are separate surfaces:**
  - The **daily email** (`/emails/{date}` archive renders it as-sent; the
    `/emails` list is the archive index) carries `Config.max_rss_articles`
    ranked picks (RSS + Hacker News) plus `Config.max_web_articles` open-web
    results, both votable via the signed `/rate` link, plus discoveries. It
    links generically to the homepage.
  - The **homepage** (`/`, `read.html.j2`) is the rich, browse-everything web
    experience: a separate aggregation stored under the reserved issue key
    `"home"` (see `digest.HOME_KEY`). There is **no refresh button** — a
    scheduled EventBridge rule rebuilds it daily at 09:45 UTC (a
    `{"home": true}` digest run, 15 min before the email), and the page also
    auto-regenerates on demand when the stored edition is missing or not from
    today (client kicks `/api/home/refresh` → async digest `{"home": true}`,
    polls `/api/home/status`, reloads). Voting uses `/api/vote` (admin cookie),
    same `FeedbackRow` shape as `/rate`; an **upvote** is sticky and a
    **downvote removes** the article from the page (and it stays gone — the
    home view drops already-downvoted articles).
- **Admin config** lives in the profile table under `id="config"`
  (`db.get_config`/`put_config`, model `contracts.Config`): `max_rss_articles`,
  `max_web_articles`, and `web_variety` (0–100 exploration dial for
  `websearch.search_web`). Read leniently (defaults on a missing/bad row).
- **Lenient on read, strict on write:** DB readers (`list_feeds`,
  `recent_feedback`, `get_issue`) skip-and-log bad/legacy rows rather than
  raising, so one bad row can't break a whole page. When you make a model
  field required, check the persisted-data read paths for older rows.
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
