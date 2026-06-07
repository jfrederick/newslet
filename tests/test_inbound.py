"""Tests for :mod:`newslet.handlers.inbound` — the SES inbound pipeline."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from email.message import EmailMessage

import boto3
import moto
import pytest

from newslet.config import settings


@pytest.fixture
def dynamo(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for k, v in {
        "ANTHROPIC_API_KEY": "x",
        "RESEND_API_KEY": "x",
        "FROM_EMAIL": "from@example.com",
        "TO_EMAIL": "to@example.com",
        "ADMIN_TOKEN": "x",
        "SIGNING_KEY": "x",
        "MAIL_DOMAIN": "inbox.example.com",
        "INBOX_BUCKET": "newslet-inbox-bucket",
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
    }.items():
        monkeypatch.setenv(k, v)
    settings.cache_clear()
    with moto.mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="newslet-subscriptions",
            KeySchema=[{"AttributeName": "address", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "address", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName="newslet-inbox",
            KeySchema=[{"AttributeName": "message_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "message_id", "AttributeType": "S"},
                {"AttributeName": "bucket", "AttributeType": "S"},
                {"AttributeName": "received_at", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "inbox-by-ts",
                    "KeySchema": [
                        {"AttributeName": "bucket", "KeyType": "HASH"},
                        {"AttributeName": "received_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield
    settings.cache_clear()


def _email(*, to: str, subject: str, html: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = "Newsletter <hi@news.example.com>"
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = "Tue, 02 Jun 2026 09:00:00 +0000"
    msg.set_content("plain text version")
    msg.add_alternative(html, subtype="html")
    return msg.as_bytes()


def test_process_message_stores_articles(dynamo):
    from newslet import db
    from newslet.handlers import inbound

    sub = db.add_subscription("The Daily", address="n-abc@inbox.example.com")
    raw = _email(
        to=sub.address,
        subject="This week in tech",
        html="""
          <a href="https://site.example/story-one">A genuinely interesting headline</a>
          <a href="https://site.example/unsubscribe">Unsubscribe</a>
        """,
    )
    now = datetime(2026, 6, 2, 9, 30, tzinfo=UTC)

    result = inbound.process_message(
        raw, [sub.address], "msg-1", confirm=lambda _u: True, now=now
    )

    assert result["status"] == "stored"
    assert result["articles"] == 1
    # The extracted article shows up as a recent inbox candidate.
    arts = db.recent_inbox_articles(datetime(2026, 6, 1, tzinfo=UTC), now=now)
    assert [str(a.url) for a in arts] == ["https://site.example/story-one"]
    assert arts[0].source == "The Daily"
    # last_received_at recorded.
    assert db.get_subscription(sub.address).last_received_at is not None


def test_process_message_auto_confirms(dynamo):
    from newslet import db
    from newslet.handlers import inbound

    sub = db.add_subscription("Stratechery", address="n-xyz@inbox.example.com")
    assert sub.status == "pending"
    raw = _email(
        to=sub.address,
        subject="Please confirm your subscription",
        html='<a href="https://news.example/c/token123">Confirm your subscription</a>',
    )

    followed: list[str] = []

    def fake_confirm(url: str) -> bool:
        followed.append(url)
        return True

    result = inbound.process_message(raw, [sub.address], "msg-2", confirm=fake_confirm)

    assert result["status"] == "confirmed"
    assert followed == ["https://news.example/c/token123"]
    updated = db.get_subscription(sub.address)
    assert updated.status == "confirmed"
    assert updated.confirmed_at is not None
    # A confirmation email yields no stored articles.
    assert db.recent_inbox_articles(datetime(2026, 1, 1, tzinfo=UTC)) == []


def test_process_message_confirm_failure_stays_pending(dynamo):
    from newslet import db
    from newslet.handlers import inbound

    sub = db.add_subscription("X", address="n-fail@inbox.example.com")
    raw = _email(
        to=sub.address,
        subject="Confirm your email",
        html='<a href="https://news.example/verify/abc">Verify</a>',
    )
    result = inbound.process_message(raw, [sub.address], "m", confirm=lambda _u: False)
    assert result["status"] == "confirm_failed"
    assert db.get_subscription(sub.address).status == "pending"


def test_process_message_no_matching_subscription(dynamo):
    from newslet.handlers import inbound

    raw = _email(
        to="n-unknown@inbox.example.com",
        subject="hi",
        html='<a href="https://s.example/p">a headline here that is long</a>',
    )
    result = inbound.process_message(raw, ["n-unknown@inbox.example.com"], "m")
    assert result["status"] == "no_match"


def test_process_message_matches_via_recipient_case_insensitively(dynamo):
    from newslet import db
    from newslet.handlers import inbound

    sub = db.add_subscription("Y", address="n-case@inbox.example.com")
    raw = _email(
        to="someone-else@elsewhere.example",  # header doesn't match
        subject="news",
        html='<a href="https://s.example/p">a sufficiently long headline here</a>',
    )
    # SES hands us the real recipient, upper-cased — must still match.
    result = inbound.process_message(raw, ["N-CASE@INBOX.EXAMPLE.COM"], "m3")
    assert result["status"] == "stored"
    assert result["address"] == sub.address


def test_process_message_misclassified_confirmation_still_extracts(dynamo):
    from newslet import db
    from newslet.handlers import inbound

    sub = db.add_subscription("Daily", address="n-fp@inbox.example.com")
    assert sub.status == "pending"
    # Confirmation-shaped subject but the only link is a real article — must
    # fail safe toward extraction rather than dropping the issue.
    raw = _email(
        to=sub.address,
        subject="Confirm you caught this week's stories",
        html='<a href="https://site.example/story-one">A genuinely interesting headline</a>',
    )
    result = inbound.process_message(
        raw, [sub.address], "fp-1", confirm=lambda _u: True
    )
    assert result["status"] == "stored"
    assert result["articles"] == 1


def test_process_message_confirmed_sub_still_extracts(dynamo):
    from newslet import db
    from newslet.handlers import inbound

    sub = db.add_subscription("S", address="n-cf@inbox.example.com")
    db.mark_subscription_confirmed(sub.address, when=datetime(2026, 6, 1, tzinfo=UTC))
    raw = _email(
        to=sub.address,
        subject="Confirm your subscription",
        html='<a href="https://site.example/story-one">A genuinely interesting headline</a>',
    )
    called: list[str] = []
    result = inbound.process_message(
        raw, [sub.address], "cf-1", confirm=lambda u: called.append(u) or True
    )
    assert result["status"] == "stored"
    assert result["articles"] == 1
    assert called == []


def test_follow_link_refuses_non_public_and_bad_scheme():
    from newslet.handlers import inbound

    assert inbound._is_public_url("http://127.0.0.1/confirm") is False
    assert inbound._is_public_url("http://10.0.0.1/confirm") is False
    assert inbound._is_public_url("http://169.254.169.254/latest/meta-data") is False
    assert inbound._is_public_url("ftp://example.com/x") is False
    assert inbound._is_public_url("http://93.184.216.34/confirm") is True


def test_handler_isolates_record_failures(dynamo, monkeypatch):
    from newslet.handlers import inbound

    # First record's loader raises; the handler must swallow it and keep going.
    def loader(message_id: str) -> bytes:
        if message_id == "bad":
            raise RuntimeError("S3 down")
        return _email(
            to="n-h@inbox.example.com",
            subject="x",
            html='<a href="https://s.example/p">a headline long enough here</a>',
        )

    monkeypatch.setattr(inbound, "_load_raw", loader)
    from newslet import db

    db.add_subscription("H", address="n-h@inbox.example.com")

    event = {
        "Records": [
            {"ses": {"mail": {"messageId": "bad"}, "receipt": {"recipients": ["x"]}}},
            {
                "ses": {
                    "mail": {"messageId": "ok"},
                    "receipt": {"recipients": ["n-h@inbox.example.com"]},
                }
            },
        ]
    }
    result = inbound.handler(event, None)
    assert result["processed"] == 2
    statuses = [r["status"] for r in result["results"]]
    assert statuses == ["error", "stored"]
