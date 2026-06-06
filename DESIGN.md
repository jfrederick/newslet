# newslet — Internal Design Contract

This file is the source of truth for module interfaces. Every module
listed below must export exactly the functions named here with the
signatures shown. Tests rely on these names.

## Modules

### `newslet.tokens`

HMAC-based signing for rate links from emails.

```python
def sign(article_url: str, issue_date: str) -> str: ...
def verify(article_url: str, issue_date: str, token: str) -> bool: ...
```

- `sign` returns a URL-safe base64 HMAC-SHA256 over `f"{article_url}|{issue_date}"`
  using `settings().signing_key`.
- `verify` is constant-time. Returns False on any failure, never raises.
- Tokens do not expire on their own — `issue_date` bounds replay scope.

### `newslet.feeds`

Pure fetch + filter. No DynamoDB access.

```python
def fetch_recent(
    feed_urls: list[str],
    since: datetime,
    is_seen: Callable[[str], bool],
) -> list[Article]: ...
```

- Calls `feedparser.parse` on each URL (network).
- Returns items with `published >= since` and `is_seen(url) is False`.
- Silently skips feeds that fail to parse (logs a warning); never raises
  on a single bad feed.
- Caller (digest handler) is responsible for `since` and `is_seen`.

### `newslet.db`

Thin DynamoDB wrappers. Each function uses `boto3.resource("dynamodb")`
internally and reads table names from `settings()`.

```python
# Feeds
def list_feeds() -> list[Feed]: ...
def add_feed(url: str, title: str = "") -> Feed: ...
def delete_feed(url: str) -> None: ...

# Profile (single row, pk id="me")
def get_profile() -> Profile: ...                # returns empty markdown if absent
def put_profile(markdown: str) -> Profile: ...

# Config (admin knobs; shares the profile table under pk id="config")
def get_config() -> Config: ...                  # defaults on missing/bad row
def put_config(config: Config) -> Config: ...

# Seen articles (pk url_hash, TTL set 21d out)
def mark_seen(urls: Iterable[str]) -> None: ...
def is_seen(url: str) -> bool: ...

# Issues
def put_issue(issue: Issue) -> None: ...
def get_issue(date: str) -> Issue | None: ...

# Feedback
def put_feedback(row: FeedbackRow) -> None: ...
def recent_feedback(limit: int = 50) -> list[FeedbackRow]: ...
```

URLs are hashed with `hashlib.sha256(url.encode()).hexdigest()` for the
`SeenArticles` PK so URL length never matters.

### `newslet.rank`

Wraps the Anthropic call.

```python
def rank(
    profile_md: str,
    feedback: list[FeedbackRow],
    candidates: list[Article],
    *,
    client: anthropic.Anthropic | None = None,
    max_picks: int = 10,
) -> RankResponse: ...
```

- Builds a system prompt + a cached user block (profile + feedback) + a
  fresh user message (candidates). Uses `cache_control` on the stable
  block.
- Asks Claude to return JSON matching `RankResponse`. Parses with
  `RankResponse.model_validate_json(...)`. Retries once on JSON parse
  failure with a stricter instruction.
- `client` is injectable for tests.

### `newslet.hn`

Hacker News as a content-rich source via the Algolia HN Search API. The
network edge is an injected `fetch(url) -> dict`.

```python
def fetch_hn_articles(
    pages: int = 20, *, fetch=None, rank_cap: int = 120,
) -> list[Article]: ...   # ranking candidates, richest summaries, points-sorted
def fetch_hn_rich(
    pages: int = 2, *, fetch=None, limit: int = 20,
) -> list[WebArticle]: ...  # web-view panel: points/comments/thread link
```

Best-effort: a failing page is skipped, total failure returns `[]`. Text/Ask
posts (no `url`) fall back to their HN thread link.

### `newslet.websearch`

On-demand web search via Claude's `web_search` tool. Powers the digest's
"from around the web" block and the web view's subject search box. The
Anthropic `client` is injectable.

```python
def search_web(
    query: str, *, max_results: int = 20, recent: bool = True,
    client=None, exclude_hosts: list[str] | None = None,
    max_searches: int = 3, model: str | None = None, variety: int = 0,
) -> list[WebArticle]: ...
```

Returns `[]` on any failure (best-effort). Reuses `discovery`'s JSON
extraction helpers. `variety` (0–100) is the admin exploration dial: low stays
on the profile, high roams into related ancillary areas (never random).
`max_searches`/`model` let the interactive subject box use a fast model and
few rounds to fit the HTTP API's ~30s limit.

### `newslet.email_render`

```python
def render_email(
    issue: Issue,
    public_base_url: str,
) -> tuple[str, str]:  # (subject, html)
    ...
```

- Loads `templates/email.html.j2`.
- Renders all stored picks plus the `web_articles` block (both votable via
  `tokens.sign(url, issue.date)` → `{base}/rate?a=…&d=…&v=up|down&t=…`), plus
  discoveries. The digest stores exactly `Config.max_rss_articles` picks and
  `Config.max_web_articles` web articles, so the email length follows config.
- Footer links generically to the homepage (`{base}/`).
- Subject: `f"newslet — {issue.date}"` unless the issue carries one.

### `newslet.handlers.digest`

Lambda entry point + CLI dry-run.

`handler` runs the daily pipeline by default; `event={"manual": true}` does an
isolated send-now and `event={"home": true}` rebuilds the homepage aggregation
(stored under `HOME_KEY="home"`, no email). Two EventBridge schedules drive it:
the home rebuild at 09:45 UTC (`{"home": true}`) and the email digest at 10:00
UTC. `run_digest` takes `max_picks`, `max_web`, and `web_variety` (daily reads
them from `Config`; the homepage uses generous fixed counts).

```python
def handler(event: dict, context: object) -> dict: ...
def main() -> None:  # CLI for --dry-run
    ...
```

`--dry-run` reads `feeds.txt` from cwd, mocks the Anthropic client to
return a deterministic ranking of the first N articles, writes
`out/email.html`, prints the subject.

### `newslet.handlers.web`

FastAPI app wrapped with Mangum.

```python
app = FastAPI(...)
handler = Mangum(app)
```

Routes:
- `GET /` — the homepage: rich reading UX (`read.html.j2`) over the stored
  `"home"` aggregation, with a today's-date header, +/- voting (upvote sticky,
  downvote removes the article), and a subject-search box. No refresh button —
  it auto-regenerates when the stored edition is missing or not from today.
  Optional `?q=` server-renders a web search. Requires the `admin_token` cookie.
- `GET /admin` — admin UI (feeds, profile, daily-email settings, send now)
- `POST /login` — sets cookie if body token matches `settings().admin_token`
- `POST /api/feeds` — `{url, title?}` → 303 `/admin`
- `POST /api/feeds/delete` — `{url}` → 303 `/admin`
- `POST /api/profile` — `{markdown}` → 303 `/admin`
- `POST /api/config` — `{max_rss_articles, max_web_articles, web_variety}` → 303 `/admin`
- `GET /rate` — `?a=&d=&v=&t=` → "thanks" HTML; verifies `t` and writes feedback
- `GET /emails` — the sent-email archive index
- `GET /emails/{date}` — the as-sent daily email HTML (archive view)
- `POST /api/vote` — `{url, title?, rating, date}`, admin-cookie authed; writes
  a `FeedbackRow` (same shape as `/rate`). JSON for fetch UI, 303 `/` for no-JS.
- `GET /api/search` — `?q=` admin-authed live web search → JSON cards
- `GET /api/hn` — admin-authed live Hacker News front page → JSON cards
- `POST /api/home/refresh` — async-invoke digest `{"home": true}` → JSON
- `GET /api/home/status` — `{created_at, ready}` for the refresh poll

## DynamoDB tables

| Table | PK | SK | Other attrs | TTL |
|---|---|---|---|---|
| `newslet-feeds` | `url` (S) | — | `title`, `added_at` | no |
| `newslet-profile` | `id` (S: `"me"` profile, `"config"` admin knobs) | — | `markdown`/counts, `updated_at` | no |
| `newslet-seen-articles` | `url_hash` (S) | — | `url`, `expires_at` (N) | `expires_at` |
| `newslet-issues` | `date` (S) | — | `picks_json`, `created_at`, `subject`, `intro`, `discoveries_json`, `web_articles_json` | no |
| `newslet-feedback` | `article_url` (S) | `ts` (S, ISO8601) | `title`, `rating` | no |

## Claude prompt JSON shape

Claude returns **only** this JSON (no prose):

```json
{
  "picks": [
    {
      "url": "https://…",
      "title": "…",
      "blurb": "one-sentence why-this-matters",
      "source": "Feed Title",
      "score": 0.0
    }
  ]
}
```

`score` is 0.0–1.0; the email orders picks by descending score.
