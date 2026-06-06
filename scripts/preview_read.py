"""Render the rich issue web view to ``out/read.html`` (no network, no AWS).

Seeds a moto-backed DynamoDB with a realistic issue (40 ranked picks + 20
"from around the web" articles, including Hacker News items with engagement
metadata), drives the real FastAPI app through a TestClient, and writes the
rendered page so you can eyeball the layout — the web-view analogue of
``scripts/dry_run.py`` for the email.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

for k, v in {
    "ANTHROPIC_API_KEY": "preview",
    "RESEND_API_KEY": "preview",
    "FROM_EMAIL": "newslet@example.com",
    "TO_EMAIL": "you@example.com",
    "ADMIN_TOKEN": "preview-token",
    "SIGNING_KEY": "preview-signing-key",
    "AWS_REGION": "us-east-1",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
}.items():
    os.environ.setdefault(k, v)

import boto3  # noqa: E402
import moto  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from newslet.config import settings  # noqa: E402
from newslet.contracts import FeedbackRow, Issue, Pick, WebArticle  # noqa: E402

_SOURCES = ["The Verge", "Stratechery", "Hacker News", "Nature", "LessWrong", "Quanta"]


def _make_issue(date: str) -> Issue:
    picks = []
    for i in range(40):
        src = _SOURCES[i % len(_SOURCES)]
        picks.append(
            Pick(
                url=f"https://example.com/pick/{i}",
                title=f"Ranked pick #{i + 1}: a genuinely interesting headline",
                blurb=(
                    "A one-sentence synopsis of why this matters, written by the "
                    "ranker against your profile."
                ),
                source=src,
                score=round(1.0 - i * 0.02, 3),
            )
        )
    web = []
    for i in range(20):
        is_hn = i % 3 == 0
        web.append(
            WebArticle(
                url=f"https://web.example.com/article/{i}",
                title=f"From around the web #{i + 1}: a fresh find",
                blurb="Pulled live from the open web for the rich view.",
                source="Hacker News" if is_hn else "Open Web",
                points=(420 - i * 7) if is_hn else None,
                comments=(180 - i * 4) if is_hn else None,
                comments_url=(
                    f"https://news.ycombinator.com/item?id={1000 + i}" if is_hn else ""
                ),
            )
        )
    return Issue(
        date=date,
        picks=picks,
        created_at=datetime.now(UTC),
        subject="Today's read: 60 articles, ranked and pulled from the web",
        intro=(
            "Forty ranked picks from your feeds and Hacker News, plus twenty more "
            "pulled live from the open web. Vote to tune tomorrow's ranking."
        ),
        web_articles=web,
    )


def main() -> int:
    settings.cache_clear()
    with moto.mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="newslet-issues",
            KeySchema=[{"AttributeName": "date", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "date", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName="newslet-feedback",
            KeySchema=[
                {"AttributeName": "article_url", "KeyType": "HASH"},
                {"AttributeName": "issue_date", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "article_url", "AttributeType": "S"},
                {"AttributeName": "issue_date", "AttributeType": "S"},
                {"AttributeName": "bucket", "AttributeType": "S"},
                {"AttributeName": "ts", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "feedback-by-ts",
                    "KeySchema": [
                        {"AttributeName": "bucket", "KeyType": "HASH"},
                        {"AttributeName": "ts", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        from newslet import db
        from newslet.handlers.web import app

        date = datetime.now(UTC).strftime("%Y-%m-%d")
        db.put_issue(_make_issue(date))
        # Seed a couple of votes so the sticky state is visible in the preview.
        db.put_feedback(FeedbackRow(article_url="https://example.com/pick/0",
                                    title="x", rating="up", ts=datetime.now(UTC),
                                    issue_date=date))
        db.put_feedback(FeedbackRow(article_url="https://example.com/pick/3",
                                    title="x", rating="down", ts=datetime.now(UTC),
                                    issue_date=date))

        client = TestClient(app)
        client.cookies.set("admin_token", "preview-token")
        resp = client.get(f"/issues/{date}")
        resp.raise_for_status()

        out = Path(__file__).resolve().parent.parent / "out" / "read.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(resp.text, encoding="utf-8")
        print(f"wrote {out} ({len(resp.text)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
