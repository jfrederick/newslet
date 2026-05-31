# Send email now — admin button

## Goal

Add a button to the admin interface that triggers a real digest run on
demand. The run sends a genuine email with working rate links (the full
feedback loop is live), but it stays invisible to the daily cadence: it
does not appear in "recent issues" and does not count toward timing of
any kind.

## Background

The system has two Lambdas (see `infra/template.yaml`):

- **DigestFunction** (`newslet.handlers.digest.handler`, 300s/1024MB) —
  runs the daily pipeline on an EventBridge cron and is the only place
  with the time/memory budget to fetch → rank → summarize → discover →
  send → tune.
- **WebFunction** (`newslet.handlers.web.handler`, 30s/512MB) — the admin
  UI and the public `/rate` endpoint.

Issues are keyed by `date` (a plain string — `contracts.Issue.date` has
no format validation). That same string is reused in three places:

1. the DynamoDB issues-table primary key,
2. the `d=` query param in every rate link (`email_render`),
3. the HMAC-signed `(article_url, date)` token (`tokens.sign`).

"Timing" state lives in:

- `db.issue_sent(today)` — the daily idempotency gate.
- `db.mark_issue_sent` — flips the `sent_at` marker.
- `db.list_issues` / `last_sent` — what the admin index shows.

## Design

### Manual-mode digest run

`digest.handler(event, context)` branches on `event.get("manual")`.

A manual run is identical to a real run **except**:

| Concern | Real run | Manual run |
|---|---|---|
| Issue key | today's `YYYY-MM-DD` | synthetic `manual-YYYYMMDD-HHMMSS` (UTC) |
| `issue_sent(today)` check | gates the run | skipped (distinct key can't collide) |
| `mark_issue_sent` | sets today's `sent_at` | not called for today's date |
| `mark_seen` | marks all candidates seen | **skipped** — leaves the seen-store untouched so the scheduled digest is unaffected |
| profile auto-tune | runs (best effort) | **runs** (best effort) — faithful to a real run |
| `list_issues` visibility | shown | hidden via a `manual` row attribute |

The synthetic key is URL-safe (letters, digits, hyphens), so it flows
cleanly into rate-link `d=` params and the HMAC token. A `+`/`-` click
records a `FeedbackRow` keyed on the synthetic `issue_date`; since
`recent_feedback` reads by year/timestamp bucket (not by `issue_date`),
that feedback feeds future ranking and tuning exactly like any other.

`handler` is refactored so the send tail (render → `_send_email` →
post-send steps) is shared rather than duplicated across the two modes.

### Storage / listing

- `db.put_issue(issue, *, manual=False)` writes a `manual` boolean to the
  row when true.
- `db.list_issues` filters out rows whose `manual` attribute is truthy,
  so neither "recent issues" nor the derived `last_sent` ever sees a
  manual send. The full issue is still retrievable by `get_issue(key)`,
  so `/rate` title lookup and `/issues/{key}` viewing continue to work.

### Web → digest invocation (async)

- New admin route `POST /api/send-now` invokes the digest Lambda
  asynchronously (`InvocationType="Event"`) with payload
  `{"manual": true}`, then redirects to `/?sent=1`. Async because a
  digest run far exceeds the web Lambda's 30s timeout; the button
  returns immediately and the email arrives shortly after.
- `config.Settings` gains an optional `digest_function_name` (env
  `DIGEST_FUNCTION_NAME`, empty default). If unset, `/api/send-now`
  returns a clear 503 rather than a vague boto error.
- `admin.html.j2` gets a "Send email now" button posting to the route,
  plus a small confirmation flash when `?sent=1` is present.

### Infrastructure

- `template.yaml`: set `DIGEST_FUNCTION_NAME: !Ref DigestFunction` on
  WebFunction and add `LambdaInvokePolicy: FunctionName: !Ref
  DigestFunction` so the web Lambda may invoke the digest Lambda.

## Testing

- **db**: `put_issue(manual=True)` writes the flag; `list_issues`
  excludes manual rows but `get_issue` still returns them.
- **digest**: a `{"manual": true}` event uses a `manual-`-prefixed key,
  does not call `mark_issue_sent` for today, does not call `mark_seen`,
  does run tuning, and stores the issue with `manual=True`; a manual run
  proceeds even when `issue_sent(today)` is true.
- **web**: `POST /api/send-now` requires admin auth, invokes the Lambda
  with the expected payload (boto client mocked), and 503s when
  `digest_function_name` is unset.

## Out of scope

- Synchronous in-request digest generation (exceeds the web timeout).
- A separate manual-issues table or TTL on manual issues.
- Surfacing manual sends anywhere in the UI.
