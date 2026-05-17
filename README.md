# newslet

A personal daily RSS newsletter. Every morning at 6am UTC, a Lambda
fetches the last 24h from your RSS feeds, asks Claude to rank and
summarize them against a profile you maintain, and emails you the top
~10 picks via Resend. Each pick has a `+` / `−` button you can tap
from your inbox; clicks land in DynamoDB and become examples in
tomorrow's prompt.

## Architecture

```
EventBridge cron (06:00) ──▶ digest Lambda ──▶ Resend (email)
                                  │
                                  ▼
                              DynamoDB ◀── web Lambda ◀── HTTP API
                                                              │
                                                              └─ admin UI + /rate
```

Five DynamoDB tables (`Feeds`, `Profile`, `SeenArticles` w/ 21d TTL,
`Issues`, `Feedback`), two Lambdas, one HTTP API. SAM-deployed.

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
```

## Deploying

You'll need the AWS CLI and SAM CLI (`brew install aws-sam-cli`),
plus accounts at [Resend](https://resend.com) and
[Anthropic](https://console.anthropic.com).

### 1. Verify your sender domain in Resend

Create a domain in the Resend dashboard, add the DNS records, and
note the `FROM_EMAIL` you want to send from (e.g. `newslet@yourdomain.com`).

### 2. Put four secrets in SSM Parameter Store

```bash
REGION=us-east-1

aws ssm put-parameter --region $REGION --type SecureString \
  --name /newslet/anthropic-api-key --value 'sk-ant-...'
aws ssm put-parameter --region $REGION --type SecureString \
  --name /newslet/resend-api-key --value 're_...'
aws ssm put-parameter --region $REGION --type SecureString \
  --name /newslet/admin-token --value "$(openssl rand -hex 32)"
aws ssm put-parameter --region $REGION --type SecureString \
  --name /newslet/signing-key --value "$(openssl rand -hex 32)"

aws ssm put-parameter --region $REGION --type String \
  --name /newslet/from-email --value 'newslet@yourdomain.com'
aws ssm put-parameter --region $REGION --type String \
  --name /newslet/to-email --value 'you@yourdomain.com'
aws ssm put-parameter --region $REGION --type String \
  --name /newslet/claude-model --value 'claude-opus-4-7'
# Placeholder; overwritten with the real API URL after first deploy
aws ssm put-parameter --region $REGION --type String \
  --name /newslet/public-base-url --value 'https://placeholder.example.com'
```

### 3. First deploy

```bash
cd infra
sam build
sam deploy --guided
```

Accept the defaults. Note the `ApiUrl` in the outputs — that's your
admin UI and rate-link base.

### 4. Set `PUBLIC_BASE_URL` to the real URL

```bash
aws ssm put-parameter --region $REGION --type String --overwrite \
  --name /newslet/public-base-url --value 'https://<api-id>.execute-api.us-east-1.amazonaws.com'
```

Then re-deploy so the Lambdas pick up the new value:

```bash
sam deploy
```

### 5. Configure your feeds and profile

Visit `https://<api-id>.execute-api.us-east-1.amazonaws.com/` in a
browser, enter your `ADMIN_TOKEN`, add some RSS feed URLs, and write
a short profile in markdown describing what you care about.

### 6. Smoke-test the digest

```bash
aws lambda invoke --region $REGION \
  --function-name newslet-DigestFunction-... \
  /tmp/out.json && cat /tmp/out.json
```

You should receive the email within seconds and see `+`/`−` links you
can click.

### 7. Wait for tomorrow

EventBridge fires the digest Lambda at 10:00 UTC daily. Adjust the cron
expression in `infra/template.yaml` if you want a different time of day
(EventBridge cron expressions are always UTC).

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

- `src/newslet/config.py` — env-driven `Settings`
- `src/newslet/contracts.py` — pydantic models (Article, Pick, Issue, FeedbackRow, …)
- `src/newslet/tokens.py` — HMAC sign/verify for `/rate` links
- `src/newslet/feeds.py` — feedparser wrapper, 24h filter, dedup via injected `is_seen`
- `src/newslet/db.py` — boto3 DynamoDB wrappers
- `src/newslet/rank.py` — Anthropic call with prompt caching
- `src/newslet/email_render.py` — Jinja → `(subject, html)`
- `src/newslet/handlers/digest.py` — scheduled Lambda + dry-run CLI
- `src/newslet/handlers/web.py` — FastAPI + Mangum (admin UI, `/rate`)
- `infra/template.yaml` — SAM stack
- `DESIGN.md` — interface contract every module follows
