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

### `newslet.email_render`

```python
def render_email(
    issue: Issue,
    public_base_url: str,
) -> tuple[str, str]:  # (subject, html)
    ...
```

- Loads `templates/email.html.j2`.
- For each pick, computes `tokens.sign(pick.url, issue.date)` and builds
  `{public_base_url}/rate?a=<url-encoded url>&d=<date>&v=up|down&t=<token>`.
- Subject: `f"newslet — {issue.date}"`.

### `newslet.handlers.digest`

Lambda entry point + CLI dry-run.

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
- `GET /` — admin UI (requires `admin_token` cookie)
- `POST /login` — sets cookie if body token matches `settings().admin_token`
- `POST /api/feeds` — `{url, title?}` → 201
- `DELETE /api/feeds` — `?url=…` → 204
- `PUT /api/profile` — `{markdown}` → 200
- `GET /rate` — `?a=&d=&v=&t=` → "thanks" HTML; verifies `t` and writes feedback
- `GET /issues/{date}` — renders the stored issue using `email_render`

## DynamoDB tables

| Table | PK | SK | Other attrs | TTL |
|---|---|---|---|---|
| `newslet-feeds` | `url` (S) | — | `title`, `added_at` | no |
| `newslet-profile` | `id` (S, always `"me"`) | — | `markdown`, `updated_at` | no |
| `newslet-seen-articles` | `url_hash` (S) | — | `url`, `expires_at` (N) | `expires_at` |
| `newslet-issues` | `date` (S) | — | `picks_json`, `created_at` | no |
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
