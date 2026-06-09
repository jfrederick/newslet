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

# Newsletter subscriptions (inbound-email source)
def add_subscription(source: str, *, address: str) -> Subscription: ...
def list_subscriptions() -> list[Subscription]: ...
def get_subscription(address: str) -> Subscription | None: ...   # case-insensitive
def delete_subscription(address: str) -> None: ...
def mark_subscription_confirmed(address: str, *, when=None) -> None: ...
def touch_subscription(address: str, *, when=None) -> None: ...

# Inbox (received newsletters → extracted Article candidates, 30d TTL)
def put_inbox_email(*, message_id, source, address, articles, received_at) -> None: ...
def recent_inbox_articles(since: datetime, *, now=None) -> list[Article]: ...
```

Addresses are stored lowercased so inbound matching is case-insensitive
regardless of how SES/the sender cases the recipient.

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

### `newslet.search_common`

Shared primitives for Claude server-side `web_search` calls, used by both
`discovery` and `websearch` (so neither reaches into the other's internals).

```python
def web_search_tool(max_uses: int = 5) -> dict: ...   # tool def; max_uses floored at 1
def last_text_block(content: list) -> str | None: ... # final text block (tool use interleaves)
def extract_json_object(text: str) -> str | None: ... # dig JSON out of fenced/prose replies
def host_key(url: str) -> str: ...                     # lowercased, www-stripped host for dedup
```

`extract_json_object` returns the first balanced `{...}` span (ignoring braces
inside string literals), preferring a fenced object when present, so a model
reply wrapped in prose or a ` ```json ` fence still parses. `host_key` is a
host-level backstop, not a true eTLD+1 extractor.

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

Returns `[]` on any failure (best-effort). Shares the `web_search` tool
definition and JSON-extraction helpers with `discovery` via
`newslet.search_common`. `variety` (0–100) is the admin exploration dial: low stays
on the profile, high roams into related ancillary areas (never random).
`max_searches`/`model` let the interactive subject box use a fast model and
few rounds to fit the HTTP API's ~30s limit.

### `newslet.x_grok`

X (Twitter) as a ranking-pool source via xAI's Grok **`x_search` tool** (the
Agent Tools API on `POST /v1/responses`; the older Live Search API was retired
2026-01-12). Returns `Article` candidates that compete with RSS/HN for the
day's picks. The network edge is an injected `complete(payload, api_key) -> dict`
(one Responses request → parsed JSON), so no new SDK dependency and tests stay
offline.

```python
def fetch_x_articles(
    query: str, *, max_results: int = 15, recent: bool = True,
    api_key: str | None = None, model: str | None = None,
    complete=None, now: datetime | None = None,
) -> list[Article]: ...
```

Best-effort: returns `[]` when no `XAI_API_KEY` is configured (the source is
simply disabled — no network call) and on any error/empty reply. Reuses
`discovery._extract_json_object` for the model reply. Each post becomes an
`Article` with `source="X"`, an engagement-rich `summary`
(likes/reposts + text), and `published=now`.

### `newslet.newsletters`

Pure parsing of inbound newsletter email into ranking candidates, plus
double-opt-in handling. No DynamoDB; no network (the confirm-link *follow*
lives in the inbound handler, where it is injectable).

```python
def generate_address(domain: str) -> str: ...        # n-<hex>@domain; raises if no domain
def parse_email(raw: bytes) -> ParsedEmail: ...       # never raises
def extract_links(parsed: ParsedEmail) -> list[tuple[str, str]]: ...   # (url, anchor)
def extract_articles(parsed, source, *, now=None, max_articles=30) -> list[Article]: ...
def is_confirmation(parsed: ParsedEmail) -> bool: ...
def find_confirmation_link(parsed: ParsedEmail) -> str | None: ...
```

Extraction is heuristic and lenient: it keeps headline-shaped links from the
HTML body (or bare URLs from a plain-text-only body), drops chrome
(unsubscribe / preferences / social / "view in browser"), dedupes, and uses the
message Date (or `now`) as each candidate's `published` so the digest's 24h
window includes it.

### `newslet.handlers.inbound`

SES-invoked Lambda entry point for received newsletter mail.

```python
def handler(event: dict, context: object) -> dict: ...   # never raises per-record
def process_message(
    raw: bytes, recipients: list[str], message_id: str,
    *, confirm=_follow_link, now=None,
) -> dict: ...   # core, network-injectable
```

`handler` loads each message's raw MIME from S3 (`inbound/<messageId>`), matches
the recipient to a `Subscription`, and either auto-follows a confirmation link
(`mark_subscription_confirmed`) or extracts links and stores them
(`put_inbox_email` + `touch_subscription`). One record's failure is logged and
swallowed so SES does not retry the whole batch.

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
them from `Config`; the homepage uses generous fixed counts), and folds in the
HN, subscribed-newsletter, and X (`x_fn`) sources — each best-effort and
seen-filtered — alongside the RSS candidates.

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
- `GET /docs` — public product guide: the attractive HTML viewer
  (`newslet/docs/index.html`) that pulls the markdown live and offers three
  selectable technical-detail levels. Linked from `/admin`.
- `GET /docs/content.md` — the canonical product-guide markdown
  (`newslet/docs/product.md`), served as `text/markdown` for the viewer to fetch
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
- `POST /api/subscriptions` — `{source}` → mints an address (needs `MAIL_DOMAIN`) → 303 `/admin`
- `POST /api/subscriptions/delete` — `{address}` → 303 `/admin`
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
| `newslet-subscriptions` | `address` (S, lowercased) | — | `source`, `status`, `created_at`, `confirmed_at`, `last_received_at` | no |
| `newslet-inbox` | `message_id` (S) | — | `received_at`, `source`, `address`, `articles_json`, `bucket` (year), `expires_at` (N) | `expires_at` (30d) |

`newslet-inbox` has a GSI `inbox-by-ts` (HASH `bucket` = year, RANGE
`received_at`) so `recent_inbox_articles` reads a time range without a scan —
the same shard pattern as `feedback-by-ts`.

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
