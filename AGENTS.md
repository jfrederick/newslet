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

## Architecture map

| Module | Responsibility |
| --- | --- |
| `config.py` | `Settings` — env vars + SSM SecureString lookups for secrets |
| `contracts.py` | pydantic models at every JSON/DB boundary (Article, Pick, Issue, Discovery, …) |
| `tokens.py` | HMAC sign/verify for `/rate` links |
| `feeds.py` | feedparser wrapper, 24h filter, dedup via injected `is_seen` |
| `db.py` | boto3 DynamoDB wrappers (5 tables) |
| `rank.py` | Anthropic ranking call with prompt caching |
| `discovery.py` | Claude web-search for sources outside your feeds |
| `summarize.py` / `tune.py` | subject/intro writing; profile auto-tuning |
| `email_render.py` | Jinja → `(subject, html)` |
| `handlers/digest.py` | scheduled Lambda + dry-run CLI |
| `handlers/web.py` | FastAPI + Mangum (admin UI, `/rate`) |
| `infra/template.yaml` | SAM stack |

## Conventions and invariants

- **Signed email links:** `tokens.sign(article_url, issue_date)`. The issue date is
  part of every signed message and bounds replay scope. Mirror the existing
  `/rate` pattern for any new email-clickable action.
- **Best-effort enrichment:** summarize and discovery must never block a send
  — they degrade to empty on any failure. Keep new enrichment steps in the
  same `try/except → empty` shape.
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
- When adding a real network/IO call in production code, make it **injectable**
  (a callable arg, like `feeds.fetch_recent`'s `is_seen`) so tests can
  substitute a fake and stay offline.

## Git / PR workflow

- Develop on a feature branch; push with `git push -u origin <branch>`.
- Opening a PR for your changes is fine.
- CI runs `ruff` + `pytest` on every PR and, on merge to `main`, deploys via
  SAM. Don't push work that fails either gate.
