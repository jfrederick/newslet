# newslet

A personal daily RSS newsletter. Every morning at 10:00 UTC, a Lambda
fetches the last 24h from your RSS feeds, asks Claude to rank and
summarize them against a profile you maintain, and emails you the top
~10 picks via Resend. Each pick has a `+` / `−` button you can tap
from your inbox; clicks land in DynamoDB and become examples in
tomorrow's prompt.

## Architecture

```
EventBridge cron (10:00 UTC) ──▶ digest Lambda ──▶ Resend (email)
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

Subsequent deploys are just `sam build && sam deploy`.

### 4. Configure your feeds and profile

Open the `ApiUrl` from step 3 in a browser, sign in with the value of
`/newslet/admin-token`, then add RSS feeds and write a short markdown
profile of what you care about.

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

EventBridge fires the digest Lambda at 10:00 UTC daily. Change the
cron in `infra/template.yaml` if you want a different time of day
(EventBridge cron is always UTC).

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
