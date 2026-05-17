"""End-to-end integration test for the digest pipeline.

Wires the real ``feeds.fetch_recent`` (with a stubbed ``feedparser``),
the real ``rank.rank`` (with a fake Anthropic client), the real
``email_render.render_email``, and the real ``db`` (against moto)
through ``handler()`` to confirm they actually compose.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import boto3
import moto
import pytest

from newslet.config import settings


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for k, v in {
        "ANTHROPIC_API_KEY": "x",
        "RESEND_API_KEY": "x",
        "FROM_EMAIL": "from@example.com",
        "TO_EMAIL": "to@example.com",
        "ADMIN_TOKEN": "x",
        "SIGNING_KEY": "signing-key",
        "PUBLIC_BASE_URL": "https://api.example.com",
        "AWS_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(k, v)
    settings.cache_clear()
    yield
    settings.cache_clear()


@pytest.fixture
def aws(env):
    with moto.mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="newslet-feeds",
            KeySchema=[{"AttributeName": "url", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "url", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName="newslet-profile",
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName="newslet-seen-articles",
            KeySchema=[{"AttributeName": "url_hash", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "url_hash", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
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
                {"AttributeName": "ts", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "article_url", "AttributeType": "S"},
                {"AttributeName": "ts", "AttributeType": "S"},
                {"AttributeName": "bucket", "AttributeType": "S"},
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
        yield


def _build_feedparser_fixture(now: datetime):
    """Return a parsed-feed object covering today + a stale entry."""
    fresh_struct = (now - timedelta(hours=2)).utctimetuple()
    stale_struct = (now - timedelta(days=5)).utctimetuple()
    return SimpleNamespace(
        bozo=0,
        bozo_exception=None,
        feed={"title": "Test Feed"},
        entries=[
            {
                "link": "https://example.com/fresh-1",
                "title": "Fresh One",
                "summary": "summary one",
                "published_parsed": fresh_struct,
            },
            {
                "link": "https://example.com/fresh-2",
                "title": "Fresh Two",
                "summary": "summary two",
                "published_parsed": fresh_struct,
            },
            {
                "link": "https://example.com/stale",
                "title": "Stale",
                "summary": "old",
                "published_parsed": stale_struct,
            },
        ],
    )


class _FakeAnthropic:
    """Bare-minimum stand-in for anthropic.Anthropic used by rank.rank."""

    def __init__(self, reply: str):
        self._reply = reply
        self.calls: list[dict] = []

    @property
    def messages(self):
        return self

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(text=self._reply)])


def test_full_pipeline_handler_end_to_end(aws, monkeypatch):
    """Run handler() against real feeds.fetch_recent, real rank.rank, real
    email_render, real db. Only feedparser, Anthropic, and Resend are stubbed.
    """
    from newslet import db, feeds, rank
    from newslet.handlers import digest

    now = datetime.now(UTC)
    monkeypatch.setattr(
        feeds,
        "feedparser",
        SimpleNamespace(parse=lambda _u: _build_feedparser_fixture(now)),
    )

    fake_reply = json.dumps(
        {
            "picks": [
                {
                    "url": "https://example.com/fresh-1",
                    "title": "Fresh One",
                    "blurb": "why fresh one matters",
                    "source": "Test Feed",
                    "score": 0.91,
                },
                {
                    "url": "https://example.com/fresh-2",
                    "title": "Fresh Two",
                    "blurb": "why fresh two matters",
                    "source": "Test Feed",
                    "score": 0.55,
                },
            ]
        }
    )
    monkeypatch.setattr(
        rank.anthropic, "Anthropic", lambda **_: _FakeAnthropic(fake_reply)
    )

    sent: list[dict] = []
    monkeypatch.setattr(
        digest,
        "_send_email",
        lambda subject, html: sent.append({"subject": subject, "html": html}),
    )

    # Seed a feed and a profile
    db.add_feed("https://example.com/rss", title="Test Feed")
    db.put_profile("I like fresh things.")

    result = digest.handler({}, None)

    assert result["status"] == "sent"
    assert result["picks"] == 2
    assert len(sent) == 1
    assert "Fresh One" in sent[0]["html"]
    assert "Fresh Two" in sent[0]["html"]
    assert "Stale" not in sent[0]["html"]  # filtered by the 24h window

    today = now.strftime("%Y-%m-%d")
    stored = db.get_issue(today)
    assert stored is not None and len(stored.picks) == 2

    # All three candidates marked seen (including the rejected stale one
    # was NOT marked, but the two fresh + any other fresh candidates were).
    # Actually only the fresh-1 and fresh-2 were candidates (stale was
    # filtered before ranking), so only those two should be in SeenArticles.
    assert db.is_seen("https://example.com/fresh-1")
    assert db.is_seen("https://example.com/fresh-2")
    assert not db.is_seen("https://example.com/stale")


def test_handler_is_idempotent_within_a_day(aws, monkeypatch):
    """A second invocation in the same day must not send a duplicate."""
    from newslet import db, feeds, rank
    from newslet.handlers import digest

    now = datetime.now(UTC)
    monkeypatch.setattr(
        feeds,
        "feedparser",
        SimpleNamespace(parse=lambda _u: _build_feedparser_fixture(now)),
    )
    monkeypatch.setattr(
        rank.anthropic,
        "Anthropic",
        lambda **_: _FakeAnthropic(json.dumps({"picks": [
            {"url": "https://example.com/fresh-1", "title": "Fresh One",
             "blurb": "b", "source": "s", "score": 0.9},
        ]})),
    )

    sent: list[dict] = []
    monkeypatch.setattr(digest, "_send_email", lambda s, h: sent.append({"s": s, "h": h}))

    db.add_feed("https://example.com/rss")

    # First run sends
    first = digest.handler({}, None)
    assert first["status"] == "sent"
    assert len(sent) == 1

    # Second run in the same day short-circuits
    second = digest.handler({}, None)
    assert second["status"] == "already_sent"
    assert len(sent) == 1  # not incremented


def test_handler_sends_even_with_zero_picks(aws, monkeypatch):
    """An empty-candidate day should still produce an email so the user
    notices the pipeline ran (vs silently going dark)."""
    from newslet import db, feeds
    from newslet.handlers import digest

    # No entries at all → empty candidate list → no rank call needed
    monkeypatch.setattr(
        feeds,
        "feedparser",
        SimpleNamespace(parse=lambda _u: SimpleNamespace(
            bozo=0, bozo_exception=None, feed={"title": "T"}, entries=[]
        )),
    )

    sent: list[dict] = []
    monkeypatch.setattr(digest, "_send_email", lambda s, h: sent.append({"s": s, "h": h}))

    db.add_feed("https://example.com/rss")

    result = digest.handler({}, None)
    assert result["status"] == "sent"
    assert result["picks"] == 0
    assert len(sent) == 1
    assert "0 picks today" in sent[0]["h"]  # the email template's footer
