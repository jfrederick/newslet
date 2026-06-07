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

Browser end-to-end tests (`tests/e2e/`, Playwright) are **opt-in** — the
default `pytest -q` run deselects them (marker `e2e`). They drive the real
FastAPI app in Chromium, served by `uvicorn` in-process against moto-backed
DynamoDB, so they stay offline like the rest of the suite. Run them with:

```bash
.venv/bin/playwright install chromium   # one-time; downloads the browser
.venv/bin/python -m pytest -m e2e
```

CI does not yet run them — a dedicated `e2e` job (separate from the fast unit
gate, gating deploy alongside the unit job) is a pending follow-up.

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
| `db.py` | boto3 DynamoDB wrappers (5 tables) |
| `rank.py` | Anthropic ranking call with prompt caching |
| `discovery.py` | Claude web-search for sources outside your feeds |
| `summarize.py` / `tune.py` | subject/intro writing; profile auto-tuning |
| `email_render.py` | Jinja → `(subject, html)` (configurable counts; HN + web block; generic homepage link) |
| `handlers/digest.py` | scheduled Lambda + dry-run CLI; `{"manual"}` send-now and `{"home"}` homepage-rebuild modes |
| `handlers/web.py` | FastAPI + Mangum (`/` homepage, `/admin`, `/issues/{date}` email archive, `/rate`, `/api/vote`, `/api/search`, `/api/hn`, `/api/config`, `/api/home/*`) |
| `templates/read.html.j2` | the homepage: rich reading UX (voting, subject search, Refresh) |
| `templates/admin.html.j2` | admin UI (feeds, profile, daily-email settings, send now) |
| `infra/template.yaml` | SAM stack |

## Conventions and invariants

- **Signed email links:** `tokens.sign(article_url, issue_date)`. The issue date is
  part of every signed message and bounds replay scope. Mirror the existing
  `/rate` pattern for any new email-clickable action.
- **Best-effort enrichment:** summarize, discovery, the Hacker News source
  (`hn.fetch_hn_articles`), and the web-search block (`websearch.search_web`)
  must never block a send — they degrade to empty on any failure. Keep new
  enrichment steps in the same `try/except → empty` shape, and make their
  network edge injectable (HN takes a `fetch` callable; websearch a `client`;
  `run_digest` takes `hn_fn`/`websearch_fn`) so tests stay offline.
- **Email vs. homepage are separate surfaces:**
  - The **daily email** (`/issues/{date}` archive renders it as-sent) carries
    `Config.max_rss_articles` ranked picks (RSS + Hacker News) plus
    `Config.max_web_articles` open-web results, both votable via the signed
    `/rate` link, plus discoveries. It links generically to the homepage.
  - The **homepage** (`/`, `read.html.j2`) is the rich, browse-everything web
    experience: a separate aggregation stored under the reserved issue key
    `"home"` (see `digest.HOME_KEY`), regenerated on demand by the Refresh
    button (`/api/home/refresh` → async digest `{"home": true}` → poll
    `/api/home/status`). Voting uses `/api/vote` (admin cookie) writing the
    same `FeedbackRow` shape as `/rate`, so both feed the next ranking.
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
- **Match surrounding style:** comment density, naming, and idioms vary by
  file; follow the file you're editing.

## Testing patterns

- No test hits the network or real AWS. External edges are always stubbed:
  **Anthropic → `FakeClient`**, **AWS DynamoDB → moto**, **feedparser /
  resend → monkeypatched**.
- `test_integration.py` and `test_web.py` are integration-level (real modules
  composed, only the edges faked); the rest are unit tests.
- `tests/e2e/` is the Playwright browser suite (opt-in, marker `e2e`): it
  serves the app via `uvicorn` in a background thread inside the active moto
  mock and drives it in Chromium. New tests under that dir are auto-marked
  `e2e` by its `conftest.py`. Keep them offline — exercise only routes that
  read DynamoDB, never the Anthropic-backed paths.
- When adding a real network/IO call in production code, make it **injectable**
  (a callable arg, like `feeds.fetch_recent`'s `is_seen`) so tests can
  substitute a fake and stay offline.

## Git / PR workflow

- Develop on a feature branch; push with `git push -u origin <branch>`.
- Opening a PR for your changes is fine.
- CI runs `ruff` + `pytest` on every PR and, on merge to `main`, deploys via
  SAM. Don't push work that fails either gate.
