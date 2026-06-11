# newslet

A personal daily RSS newsletter. Every morning at 10:00 UTC, a Lambda
fetches the last 24h from your RSS feeds **and the Hacker News front
pages** (via the Algolia API, so stories arrive with real engagement
data, not just a title), asks Claude to rank and summarize them against
a profile you maintain, and emails you the top picks via Resend. Each
pick has a `+` / `−` button you can tap from your inbox; clicks land in
DynamoDB and become examples in tomorrow's prompt.

How many articles the email carries is configurable in the admin UI (max
RSS/HN picks and max open-web results), along with a **variety dial** that
lets the web search roam from strictly on-topic to exploratory, related
ancillary areas.

You can optionally add **X (Twitter)** as a source without paying for X's
API: if you set an xAI API key, newslet asks **Grok's `x_search` tool** for
recent posts matching your profile. They get their own **"From X" section**
in the email (and an X tab on the homepage), and they also join the daily
ranking pool alongside RSS and Hacker News so a standout post can win a top
pick. It's billed per-use by xAI (cents a day), so you skip X's flat
paid-API floor. The source stays dormant until a key is configured (see
[Optional: X (Twitter) via Grok](#optional-x-twitter-via-grok)).

You can also **subscribe to existing email newsletters** as a source: the
admin UI mints a working inbound address you paste into any newsletter's
signup form. SES receives the mail 24/7, an inbound Lambda extracts the
article links, and they join the daily ranking pool. Double opt-in
"please confirm your subscription" emails are detected and confirmed
automatically. (This is the one feature that needs a domain — see
[Optional: subscribing to newsletters](#optional-subscribing-to-newsletters).)

The email links generically to the **newslet homepage** — a separate,
richer web experience: a large aggregation of ranked picks plus an
open-web block, with `+`/`−` voting (upvote keeps, downvote removes) that
feeds the same ranking loop, and a "research a subject" box that runs a
fresh web search on whatever topic you type. The homepage has no manual
refresh button: a scheduled job rebuilds it every morning at 09:45 UTC
(15 minutes before the email), and it also regenerates on demand if the
stored edition isn't from today. Past daily emails are archived at
`/emails/<date>`.

## Product guide

A feature-by-feature **product guide** lives at
[`src/newslet/docs/product.md`](src/newslet/docs/product.md) and is served as an
attractive, self-contained HTML page at **`/docs`** (linked from the admin UI).
The page pulls the markdown in real time and lets the reader pick how much
technical detail to see — *none*, *some*, or *more* — so the same document works
for a curious user and for an engineer. The markdown is the single source of
truth; the HTML never holds its own copy. A scheduled AI step keeps the guide in
sync with the code on every push to `main` (see
[`docs/docs-autoupdate-setup.md`](docs/docs-autoupdate-setup.md)).

## Architecture

```
EventBridge cron (09:45 UTC, home) ─┐
EventBridge cron (10:00 UTC, email) ─┴▶ digest Lambda ──▶ Resend (email)
                                           │
                                           ▼
                                       DynamoDB ◀── web Lambda ◀── HTTP API
                                       ▲                               │
                                       │                  homepage + admin + /rate
   newsletter email ──▶ SES ──▶ S3 ──▶ inbound Lambda
```

Seven DynamoDB tables (`Feeds`, `Profile`, `SeenArticles` w/ 21d TTL,
`Issues`, `Feedback`, `Subscriptions`, `Inbox` w/ 30d TTL), three Lambdas
(digest, web, inbound), one HTTP API, and — for the newsletter source —
SES inbound + an S3 bucket. SAM-deployed.

## Local development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# Run the full test suite
.venv/bin/python -m pytest -q

# Lint
.venv/bin/python -m ruff check src tests

# Render a sample email to out/email.html (no network, no AWS)
.venv/bin/python scripts/dry_run.py
open out/email.html

# Render the rich issue web view to out/read.html (moto-backed, no network)
.venv/bin/python scripts/preview_read.py
open out/read.html
```

## Deploying

You need the AWS CLI and SAM CLI ≥ 1.91 (`brew install aws-sam-cli`),
plus accounts at [Resend](https://resend.com) and
[Anthropic](https://console.anthropic.com).

### 1. Verify your sender domain in Resend

Add a domain in the Resend dashboard, set the DNS records it asks for,
and note the address you want to send from (e.g.
`newslet@yourdomain.com`).

### 2. Put the seven config values in SSM Parameter Store

```bash
REGION=us-east-1

# Secrets
aws ssm put-parameter --region $REGION --type SecureString \
  --name /newslet/anthropic-api-key --value 'sk-ant-...'
aws ssm put-parameter --region $REGION --type SecureString \
  --name /newslet/resend-api-key --value 're_...'
aws ssm put-parameter --region $REGION --type SecureString \
  --name /newslet/admin-token --value "$(openssl rand -hex 32)"
aws ssm put-parameter --region $REGION --type SecureString \
  --name /newslet/signing-key --value "$(openssl rand -hex 32)"

# Plain config
aws ssm put-parameter --region $REGION --type String \
  --name /newslet/from-email --value 'newslet@yourdomain.com'
aws ssm put-parameter --region $REGION --type String \
  --name /newslet/to-email --value 'you@yourdomain.com'
aws ssm put-parameter --region $REGION --type String \
  --name /newslet/claude-model --value 'claude-opus-4-7'
```

`PUBLIC_BASE_URL` is *not* an SSM parameter — the SAM template derives
it from the HTTP API's invoke URL at deploy time.

### 3. Deploy

```bash
cd infra
sam build
sam deploy --guided
```

Accept the defaults; pick a stack name (e.g. `newslet`); say `y` to
"create managed ECR repositories" if asked and `y` to "allow IAM role
creation". Note the `ApiUrl` and `DigestFunctionName` outputs.

Subsequent deploys are just `sam build && sam deploy` — always run
`sam build` first; a bare `sam deploy` ships the source without its
dependencies and breaks the Lambdas.

#### Optional: serve the UI on your own domain

Set `SiteDomain` (and `SiteHostedZoneId`, its Route 53 zone) in
`infra/samconfig.toml` to put the web UI on `https://<SiteDomain>` instead
of the `execute-api` URL. The template then provisions a DNS-validated ACM
certificate, the API Gateway custom domains, and the Route 53 alias records;
`www.<SiteDomain>` is also created and 301-redirects to the apex. The
domain's nameservers must already point at the Route 53 hosted zone. Emailed
links automatically switch to the custom domain too.

### 4. Configure your feeds and profile

Open the `ApiUrl` from step 3 in a browser and sign in with the value of
`/newslet/admin-token`. You land on the homepage; go to **admin** (top
nav, or `/admin`) to add RSS feeds, write a short markdown profile, and
set the daily-email article counts and web-search variety. The homepage
builds its first edition automatically on that first visit (it has no
manual refresh button).

### 5. Smoke-test the digest

```bash
aws lambda invoke --region $REGION \
  --function-name "$(aws cloudformation describe-stacks \
    --stack-name newslet --region $REGION \
    --query 'Stacks[0].Outputs[?OutputKey==`DigestFunctionName`].OutputValue' \
    --output text)" \
  /tmp/out.json && cat /tmp/out.json
```

You should see `{"status":"sent",...}` and receive the email within
seconds. Click `+` or `−` in the email — you'll get a tiny "thanks"
page back and the vote will appear in the `Feedback` table.

### 6. Wait for tomorrow

EventBridge fires the digest Lambda twice daily: a homepage rebuild at
09:45 UTC (`{"home": true}`) and the email digest at 10:00 UTC. Change
the crons in `infra/template.yaml` if you want different times of day
(EventBridge cron is always UTC).

### Optional: X (Twitter) via Grok

newslet can pull recent, on-profile posts from X — without paying for X's
API. They appear as the email's "From X" section (and the homepage's X tab)
and also join the daily ranking pool. It goes through **xAI's Grok
`x_search` tool** (the Agent Tools API), which reads X as a search source
and bills per-use (a daily digest's handful of posts costs cents), so you
avoid X's flat paid-API floor and any scraping.

It's off until you add a key. To enable it:

```bash
REGION=us-east-1
aws ssm put-parameter --region $REGION --type SecureString \
  --name /newslet/xai-api-key --value 'xai-...'
```

The digest Lambda already has permission to read `/newslet/*` and decrypt
it, so no redeploy is needed — the source switches on at the next cold
start. The model defaults to `grok-4.3` (a reasoning model — the `x_search`
tool requires one); override it with the `XAI_MODEL` env var on the digest
function for a different Grok model. With no key set, the source simply
stays empty and the digest runs exactly as before.

**What it pulls:** there's no list of accounts to follow — the X source is
driven by your **profile** (the same interests text the admin UI uses for
everything else). Grok searches recent X posts and returns the ones that
best match your profile. Every fetched post lands in the email's **"From X"
section** (votable like everything else), and each also competes in the
daily ranking like any other source — a post that wins a top pick is shown
there instead of being repeated in the section. The admin **Daily email
settings** has two X controls: a **"Pull posts from X"** toggle (on by
default once a key exists; turn it off to pause the paid source without
deleting the key) and **Max X posts** (how many to fetch — it caps both the
ranking-pool contribution and the From X section, default 15).

### Optional: subscribing to newsletters

newslet can subscribe to existing email newsletters and fold their links
into the daily ranking. This needs a domain (or subdomain) you control so
SES can receive mail — everything else is already deployed.

SES inbound is only available in some regions (`us-east-1`, `us-west-2`,
`eu-west-1`); deploy the stack in one of them for this feature.

1. **Pick a mail domain** — a dedicated subdomain is cleanest, e.g.
   `inbox.yourdomain.com`, so newsletter mail can't interfere with your
   normal email.

2. **Redeploy with the domain set** so the SES receipt rule is created. Add
   `MailDomain` to `parameter_overrides` in `infra/samconfig.toml` (alongside
   `SiteDomain`/`SiteHostedZoneId`) so it persists — otherwise the next
   merge-to-`main` auto-deploy redeploys with the empty default and tears the
   receipt rule back down. Then:

   ```bash
   cd infra
   sam build && sam deploy
   ```

3. **Verify the domain in SES** and **point its MX record at SES**:

   ```
   inbox.yourdomain.com.  MX  10  inbound-smtp.us-east-1.amazonaws.com.
   ```

   (Use your stack's region.) Add the domain-verification TXT record SES
   gives you, too.

4. **Activate the receipt rule set** (CloudFormation creates it but cannot
   mark it active):

   ```bash
   aws ses set-active-receipt-rule-set --region $REGION \
     --rule-set-name "$(aws cloudformation describe-stacks \
       --stack-name newslet --region $REGION \
       --query 'Stacks[0].Outputs[?OutputKey==`MailDomain`].OutputValue' \
       --output text >/dev/null; echo newslet-inbound)"
   ```

   (The rule set is named `<stack-name>-inbound`.)

5. **Add subscriptions in the admin UI.** Under **Newsletter
   subscriptions**, type a label and click **Generate address**. Paste the
   shown address (e.g. `n-a8f3c2d1@inbox.yourdomain.com`) into the
   newsletter's signup form. If it sends a "confirm your subscription"
   email, newslet follows the link automatically and the subscription flips
   to **confirmed**; otherwise it stays **pending** until the first mail
   arrives. From then on, that newsletter's links compete in the daily
   ranking like any other source.

Raw inbound mail lands in the `InboundEmailBucket` S3 bucket (auto-expired
after 30 days) and extracted links live in the `Inbox` table (30-day TTL).

### Optional: auto-deploy on merge to main

Once you're happy with the manual flow, follow
[`docs/github-actions-setup.md`](docs/github-actions-setup.md) (~10
minutes, one-time) to wire up GitHub Actions OIDC. After that, every
push to `main` runs `pytest` + `ruff` and, on success, runs
`sam deploy` — no static AWS keys anywhere.

## Security review

Before deploying, audit dependencies:

```bash
.venv/bin/pip install pip-audit
.venv/bin/pip-audit
```

Notable third-party runtime dependencies:
- `anthropic`, `boto3`, `fastapi`, `feedparser`, `jinja2`, `mangum`,
  `pydantic`, `python-dateutil`, `python-multipart`, `resend`

All are widely-used PyPI packages (>1M monthly downloads each).
Pin versions in `pyproject.toml` are minimums; `sam build` will
resolve a lockfile.

## Module map

- `src/newslet/config.py` — `Settings` (env vars + SSM SecureString lookups for the four secrets)
- `src/newslet/contracts.py` — pydantic models (Article, Pick, Issue, FeedbackRow, …)
- `src/newslet/tokens.py` — HMAC sign/verify for `/rate` links
- `src/newslet/feeds.py` — feedparser wrapper, 24h filter, dedup via injected `is_seen`
- `src/newslet/hn.py` — Hacker News via the Algolia API (rich content), injected `fetch`
- `src/newslet/search_common.py` — shared Claude `web_search` primitives (tool def, JSON extraction, host key) for `discovery` + `websearch`
- `src/newslet/websearch.py` — Claude `web_search` for the "from around the web" block + subject search
- `src/newslet/x_grok.py` — X (Twitter) posts via xAI Grok `x_search` (the email's "From X" section + ranking candidates; on when `XAI_API_KEY` is set)
- `src/newslet/newsletters.py` — parse inbound newsletter email → article candidates; double-opt-in handling
- `src/newslet/db.py` — boto3 DynamoDB wrappers
- `src/newslet/rank.py` — Anthropic call with prompt caching
- `src/newslet/email_render.py` — Jinja → `(subject, html)`
- `src/newslet/handlers/digest.py` — scheduled Lambda + dry-run CLI
- `src/newslet/handlers/inbound.py` — SES inbound newsletter Lambda
- `src/newslet/handlers/web.py` — FastAPI + Mangum (admin UI, `/rate`)
- `infra/template.yaml` — SAM stack
- `DESIGN.md` — interface contract every module follows
