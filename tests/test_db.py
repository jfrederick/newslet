"""Tests for :mod:`newslet.db` using moto's DynamoDB mock."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import boto3
import moto
import pytest

from newslet.config import settings
from newslet.contracts import FeedbackRow, Issue, Pick


@pytest.fixture
def dynamo(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-anthropic")
    monkeypatch.setenv("RESEND_API_KEY", "dummy-resend")
    monkeypatch.setenv("FROM_EMAIL", "from@example.com")
    monkeypatch.setenv("TO_EMAIL", "to@example.com")
    monkeypatch.setenv("ADMIN_TOKEN", "dummy-admin")
    monkeypatch.setenv("SIGNING_KEY", "dummy-signing-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("TABLE_FEEDS", "newslet-feeds")
    monkeypatch.setenv("TABLE_PROFILE", "newslet-profile")
    monkeypatch.setenv("TABLE_SEEN", "newslet-seen-articles")
    monkeypatch.setenv("TABLE_ISSUES", "newslet-issues")
    monkeypatch.setenv("TABLE_FEEDBACK", "newslet-feedback")
    settings.cache_clear()

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

    settings.cache_clear()


def test_feed_crud(dynamo: None) -> None:
    from newslet import db

    assert db.list_feeds() == []

    feed = db.add_feed("https://example.com/feed.xml", title="Example")
    assert str(feed.url) == "https://example.com/feed.xml"
    assert feed.title == "Example"

    feeds = db.list_feeds()
    assert len(feeds) == 1
    assert str(feeds[0].url) == "https://example.com/feed.xml"
    assert feeds[0].title == "Example"

    db.add_feed("https://other.example.com/rss")
    assert len(db.list_feeds()) == 2

    db.delete_feed("https://example.com/feed.xml")
    remaining = db.list_feeds()
    assert len(remaining) == 1
    assert str(remaining[0].url) == "https://other.example.com/rss"


def test_profile_default_and_roundtrip(dynamo: None) -> None:
    from newslet import db

    p = db.get_profile()
    assert p.markdown == ""
    assert isinstance(p.updated_at, datetime)

    db.put_profile("# I like coffee\n")
    got = db.get_profile()
    assert got.markdown == "# I like coffee\n"
    assert isinstance(got.updated_at, datetime)


def test_seen_mark_and_check(dynamo: None) -> None:
    from newslet import db

    url_a = "https://example.com/post/a"
    url_b = "https://example.com/post/b"
    url_unseen = "https://example.com/post/never"

    db.mark_seen([url_a, url_b])

    assert db.is_seen(url_a) is True
    assert db.is_seen(url_b) is True
    assert db.is_seen(url_unseen) is False


def test_issue_put_then_get(dynamo: None) -> None:
    from newslet import db

    picks = [
        Pick(
            url="https://example.com/1",
            title="One",
            blurb="first blurb",
            source="Example",
            score=0.9,
        ),
        Pick(
            url="https://example.com/2",
            title="Two",
            blurb="second blurb",
            source="Example",
            score=0.4,
        ),
    ]
    issue = Issue(date="2026-05-17", picks=picks, created_at=datetime.now(UTC))

    db.put_issue(issue)

    got = db.get_issue("2026-05-17")
    assert got is not None
    assert got.date == "2026-05-17"
    assert len(got.picks) == 2
    assert str(got.picks[0].url) == "https://example.com/1"
    assert got.picks[0].title == "One"
    assert got.picks[1].score == 0.4

    assert db.get_issue("1999-01-01") is None


def test_add_feed_normalizes_url(dynamo: None) -> None:
    from newslet import db

    # pydantic HttpUrl lowercases the host and appends a trailing slash
    # on bare hostnames; the stored key must use that normalized form
    # so later list/delete operations roundtrip.
    feed = db.add_feed("HTTPS://Example.COM")
    assert str(feed.url) == "https://example.com/"

    listed = db.list_feeds()
    assert [str(f.url) for f in listed] == ["https://example.com/"]


def test_delete_feed_uses_normalized_key(dynamo: None) -> None:
    from newslet import db

    db.add_feed("https://Example.com/Rss")
    # Caller passes the un-normalized form; delete should still match
    # what was stored.
    db.delete_feed("HTTPS://example.com/Rss")
    assert db.list_feeds() == []


def test_add_feed_rejects_garbage(dynamo: None) -> None:
    from pydantic import ValidationError

    from newslet import db

    with pytest.raises(ValidationError):
        db.add_feed("not-a-url")


def test_list_feeds_skips_bad_rows(dynamo: None) -> None:
    from newslet import db

    # Write one good row through the normal path
    db.add_feed("https://good.example.com/rss")

    # Inject one corrupt row directly via boto3 (simulating data from
    # an older code version that didn't validate)
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.Table("newslet-feeds").put_item(
        Item={"url": "garbage-not-a-url", "title": "", "added_at": "nope"}
    )

    # The admin page must still load
    feeds = db.list_feeds()
    assert len(feeds) == 1
    assert str(feeds[0].url) == "https://good.example.com/rss"


def test_issue_exists(dynamo: None) -> None:
    from newslet import db

    assert db.issue_exists("2026-05-17") is False
    db.put_issue(Issue(date="2026-05-17", picks=[], created_at=datetime.now(UTC)))
    assert db.issue_exists("2026-05-17") is True
    assert db.issue_exists("2026-05-18") is False


def test_feedback_put_and_recent_descending(dynamo: None) -> None:
    from newslet import db

    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
    rows = [
        FeedbackRow(
            article_url=f"https://example.com/a{i}",
            title=f"Article {i}",
            rating="up" if i % 2 == 0 else "down",
            ts=base + timedelta(minutes=i),
        )
        for i in range(5)
    ]
    for row in rows:
        db.put_feedback(row)

    recent = db.recent_feedback(limit=10)
    assert len(recent) == 5
    timestamps = [r.ts for r in recent]
    assert timestamps == sorted(timestamps, reverse=True)
    assert recent[0].ts == rows[-1].ts

    limited = db.recent_feedback(limit=2)
    assert len(limited) == 2
    limited_ts = [r.ts for r in limited]
    assert limited_ts == sorted(limited_ts, reverse=True)
