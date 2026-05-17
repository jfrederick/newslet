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


def test_handler_recovers_from_failed_send(aws, monkeypatch):
    """If the first attempt stores the issue but fails to send, the
    retry must re-send the *same* picks (not re-rank, not empty)."""
    from newslet import db, feeds, rank
    from newslet.handlers import digest

    now = datetime.now(UTC)
    monkeypatch.setattr(
        feeds,
        "feedparser",
        SimpleNamespace(parse=lambda _u: _build_feedparser_fixture(now)),
    )

    rank_calls = {"count": 0}

    def counting_anthropic(**_):
        rank_calls["count"] += 1
        return _FakeAnthropic(
            json.dumps({"picks": [
                {"url": "https://example.com/fresh-1", "title": "Fresh One",
                 "blurb": "b", "source": "Test Feed", "score": 0.9},
            ]})
        )

    monkeypatch.setattr(rank.anthropic, "Anthropic", counting_anthropic)

    # First attempt: send fails
    send_state = {"fail": True, "calls": []}

    def flaky_send(subject, html):
        send_state["calls"].append({"subject": subject, "html": html})
        if send_state["fail"]:
            raise RuntimeError("Resend down")

    monkeypatch.setattr(digest, "_send_email", flaky_send)

    db.add_feed("https://example.com/rss")

    # Attempt 1: rank runs, issue stored, send blows up
    with pytest.raises(RuntimeError, match="Resend down"):
        digest.handler({}, None)
    assert rank_calls["count"] == 1
    assert len(send_state["calls"]) == 1

    today = now.strftime("%Y-%m-%d")
    assert db.issue_exists(today)
    assert db.issue_sent(today) is False

    # Attempt 2: send works
    send_state["fail"] = False
    result = digest.handler({}, None)
    assert result["status"] == "sent"
    # Did NOT pay for another rank call — reused the stored issue
    assert rank_calls["count"] == 1
    # The second send used the same picks (re-rendered with same content)
    assert len(send_state["calls"]) == 2
    assert "Fresh One" in send_state["calls"][1]["html"]
    assert db.issue_sent(today) is True


def test_handler_does_not_mark_seen_until_after_send(aws, monkeypatch):
    """mark_seen must run only after a confirmed send. Otherwise a
    crashed retry sees its own candidates as 'already seen' and emits
    an empty newsletter."""
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
    def boom(_s, _h):
        raise RuntimeError("nope")

    monkeypatch.setattr(digest, "_send_email", boom)

    db.add_feed("https://example.com/rss")

    with pytest.raises(RuntimeError):
        digest.handler({}, None)

    # Candidates should NOT have been marked seen — otherwise a retry
    # would lose them.
    assert db.is_seen("https://example.com/fresh-1") is False
    assert db.is_seen("https://example.com/fresh-2") is False


def test_send_email_invokes_resend_correctly(aws, monkeypatch):
    """Exercise the real _send_email so a signature change in the
    resend SDK is caught here rather than at 10am UTC."""
    import resend

    from newslet.handlers import digest

    calls: list[dict] = []
    monkeypatch.setattr(resend.Emails, "send", lambda payload: calls.append(payload) or {"id": "x"})

    digest._send_email("subject line", "<p>hi</p>")

    assert len(calls) == 1
    payload = calls[0]
    assert payload["subject"] == "subject line"
    assert payload["html"] == "<p>hi</p>"
    assert payload["from"] == "from@example.com"
    assert payload["to"] == ["to@example.com"]


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
