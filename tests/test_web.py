"""Integration tests for the FastAPI web app, backed by moto DynamoDB."""

from __future__ import annotations

from urllib.parse import quote

import boto3
import moto
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("RESEND_API_KEY", "x")
    monkeypatch.setenv("FROM_EMAIL", "from@example.com")
    monkeypatch.setenv("TO_EMAIL", "to@example.com")
    monkeypatch.setenv("ADMIN_TOKEN", "supersecret")
    monkeypatch.setenv("SIGNING_KEY", "signing-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    from newslet.config import settings

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


@pytest.fixture
def client(aws):
    from newslet.handlers.web import app

    # follow_redirects=False so we can inspect the 303s.
    return TestClient(app, follow_redirects=False)


def test_unauthenticated_root_redirects_to_login(client):
    r = client.get("/")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_login_bad_token(client):
    r = client.post("/login", data={"token": "wrong"})
    assert r.status_code == 200
    assert "Invalid token" in r.text


def test_login_good_token_sets_cookie(client):
    r = client.post("/login", data={"token": "supersecret"})
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert "admin_token=supersecret" in r.headers["set-cookie"]


def test_add_and_delete_feed_roundtrip(client):
    client.cookies.set("admin_token", "supersecret")
    r = client.post(
        "/api/feeds",
        data={"url": "https://example.com/rss", "title": "Example"},
    )
    assert r.status_code == 303

    r = client.get("/")
    assert r.status_code == 200
    # Look for the delete form which only renders for actual feed rows
    assert 'value="https://example.com/rss"' in r.text

    r = client.post("/api/feeds/delete", data={"url": "https://example.com/rss"})
    assert r.status_code == 303
    r = client.get("/")
    assert 'value="https://example.com/rss"' not in r.text


def test_profile_save(client):
    client.cookies.set("admin_token", "supersecret")
    r = client.post("/api/profile", data={"markdown": "I like LLMs and Postgres."})
    assert r.status_code == 303
    r = client.get("/")
    assert "I like LLMs and Postgres." in r.text


def test_rate_rejects_unsigned_token(client):
    url = "https://example.com/article"
    r = client.get(
        "/rate",
        # TestClient percent-encodes `params` values once, mirroring what
        # a real browser does with the email link.
        params={"a": url, "d": "2026-05-17", "v": "up", "t": "garbage"},
    )
    assert r.status_code == 403


def test_rate_accepts_signed_token(client):
    from newslet import tokens

    url = "https://example.com/article"
    token = tokens.sign(url, "2026-05-17")
    r = client.get(
        "/rate",
        params={"a": url, "d": "2026-05-17", "v": "up", "t": token},
    )
    assert r.status_code == 200
    assert "thanks" in r.text.lower()


def test_rate_accepts_url_with_literal_percent_xx(client):
    """Articles whose path contains '%XX' (e.g., Wikipedia titles
    encoded with %20) must not get double-decoded by /rate."""
    from newslet import tokens

    url = "https://en.wikipedia.org/wiki/Hello%20world"
    token = tokens.sign(url, "2026-05-17")
    r = client.get(
        "/rate",
        params={"a": url, "d": "2026-05-17", "v": "up", "t": token},
    )
    assert r.status_code == 200


def test_add_feed_returns_400_on_garbage_url(client):
    client.cookies.set("admin_token", "supersecret")
    r = client.post("/api/feeds", data={"url": "not-a-url"})
    assert r.status_code == 400
    assert "invalid feed URL" in r.text


def test_add_then_delete_with_uppercase_input(client):
    """User adds a feed in one case; deletes with a different case.

    Both must hit the same normalized DynamoDB key.
    """
    client.cookies.set("admin_token", "supersecret")
    r = client.post("/api/feeds", data={"url": "HTTPS://Example.COM/Rss"})
    assert r.status_code == 303

    r = client.get("/")
    assert 'value="https://example.com/Rss"' in r.text

    r = client.post("/api/feeds/delete", data={"url": "https://EXAMPLE.com/Rss"})
    assert r.status_code == 303

    r = client.get("/")
    assert 'value="https://example.com/Rss"' not in r.text


def test_issues_index_lists_recent_issues(client):
    from datetime import UTC, datetime

    from newslet import db
    from newslet.contracts import Issue

    client.cookies.set("admin_token", "supersecret")

    db.put_issue(Issue(date="2026-05-17", picks=[], created_at=datetime.now(UTC)))
    db.put_issue(Issue(date="2026-05-16", picks=[], created_at=datetime.now(UTC)))
    db.mark_issue_sent("2026-05-16")

    r = client.get("/issues")
    assert r.status_code == 200
    assert "2026-05-17" in r.text
    assert "2026-05-16" in r.text
    # The 2026-05-16 issue should show as sent; 2026-05-17 as unsent
    assert "sent" in r.text
    assert "unsent" in r.text


def test_admin_index_shows_last_sent(client):
    from datetime import UTC, datetime

    from newslet import db
    from newslet.contracts import Issue

    client.cookies.set("admin_token", "supersecret")
    db.put_issue(Issue(date="2026-05-15", picks=[], created_at=datetime.now(UTC)))
    db.mark_issue_sent("2026-05-15")

    r = client.get("/")
    assert r.status_code == 200
    assert "Last sent" in r.text
    assert "2026-05-15" in r.text


def test_rate_thanks_page_shows_note_form(client):
    from newslet import tokens

    url = "https://example.com/article"
    token = tokens.sign(url, "2026-05-17")
    r = client.get(
        "/rate",
        params={"a": url, "d": "2026-05-17", "v": "up", "t": token},
    )
    assert r.status_code == 200
    assert 'action="/rate/note"' in r.text
    assert 'name="note"' in r.text
    # Hidden fields carry the signed token forward.
    assert f'value="{token}"' in r.text


def test_rate_note_saves_with_valid_token(client, monkeypatch):
    from newslet import db, tokens

    url = "https://example.com/article"
    token = tokens.sign(url, "2026-05-17")

    saved: list[tuple] = []
    monkeypatch.setattr(
        db, "update_feedback_note",
        lambda article_url, issue_date, note: saved.append(
            (article_url, issue_date, note)
        ),
    )

    r = client.post(
        "/rate/note",
        data={"a": url, "d": "2026-05-17", "t": token, "note": "too much crypto"},
    )
    assert r.status_code == 200
    assert saved == [(url, "2026-05-17", "too much crypto")]


def test_rate_note_uses_normalized_key(client, monkeypatch):
    """The note attaches under the same canonical key /rate stores the row at,
    even when the signed URL isn't already in canonical form."""
    from newslet import db, tokens

    raw = "https://Example.COM/Article"  # non-canonical host casing
    token = tokens.sign(raw, "2026-05-17")

    saved: list[str] = []
    monkeypatch.setattr(
        db, "update_feedback_note",
        lambda article_url, issue_date, note: saved.append(article_url),
    )

    r = client.post(
        "/rate/note",
        data={"a": raw, "d": "2026-05-17", "t": token, "note": "x"},
    )
    assert r.status_code == 200
    # Stored under the normalized key, not the raw one the link carried.
    assert saved == [db.normalize_url(raw)]
    assert saved[0] != raw


def test_rate_note_rejects_bad_token(client, monkeypatch):
    from newslet import db

    called: list = []
    monkeypatch.setattr(
        db, "update_feedback_note",
        lambda *a, **kw: called.append(a),
    )

    r = client.post(
        "/rate/note",
        data={"a": "https://example.com/article", "d": "2026-05-17",
              "t": "garbage", "note": "nope"},
    )
    assert r.status_code == 403
    assert called == []


def test_rate_rejects_bad_rating(client):
    from newslet import tokens

    url = "https://example.com/article"
    token = tokens.sign(url, "2026-05-17")
    r = client.get(
        "/rate",
        params={"a": quote(url, safe=""), "d": "2026-05-17", "v": "sideways", "t": token},
    )
    assert r.status_code == 400


def test_subscribe_rejects_unsigned_token(client):
    feed = "https://newsource.example.org/feed.xml"
    r = client.get(
        "/subscribe",
        params={"f": feed, "d": "2026-05-17", "t": "garbage", "s": "New Source"},
    )
    assert r.status_code == 403


def test_subscribe_adds_feed_with_signed_token(client):
    from newslet import db, tokens

    feed = "https://newsource.example.org/feed.xml"
    token = tokens.sign(feed, "2026-05-17")
    r = client.get(
        "/subscribe",
        params={"f": feed, "d": "2026-05-17", "t": token, "s": "New Source"},
    )
    assert r.status_code == 200
    assert "subscribed" in r.text.lower()
    # The feed now appears in the user's feeds.
    feed_urls = [str(f.url) for f in db.list_feeds()]
    assert feed in feed_urls
    # And it carries the source title supplied in the link.
    titles = {str(f.url): f.title for f in db.list_feeds()}
    assert titles[feed] == "New Source"


def test_subscribe_is_idempotent(client):
    """Clicking the same signed link twice is harmless (upsert)."""
    from newslet import db, tokens

    feed = "https://newsource.example.org/feed.xml"
    token = tokens.sign(feed, "2026-05-17")
    params = {"f": feed, "d": "2026-05-17", "t": token, "s": "New Source"}

    assert client.get("/subscribe", params=params).status_code == 200
    assert client.get("/subscribe", params=params).status_code == 200

    feed_urls = [str(f.url) for f in db.list_feeds()]
    assert feed_urls.count(feed) == 1


def test_subscribe_400_on_garbage_feed(client):
    """A signed-but-invalid feed URL fails validation as a 400, not a 500.

    (Sign over the exact same string the endpoint validates so we exercise
    the post-auth ValidationError path rather than the 403.)"""
    from newslet import tokens

    bad = "http://"  # passes HMAC but not HttpUrl
    token = tokens.sign(bad, "2026-05-17")
    r = client.get(
        "/subscribe",
        params={"f": bad, "d": "2026-05-17", "t": token},
    )
    assert r.status_code == 400


def test_send_now_requires_admin(client):
    r = client.post("/api/send-now")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_send_now_invokes_digest_lambda_async(client, monkeypatch):
    """An authenticated send-now async-invokes the digest Lambda with a
    {"manual": true} payload, then redirects back with a flash."""
    import json

    from newslet.config import settings
    from newslet.handlers import web

    monkeypatch.setenv("DIGEST_FUNCTION_NAME", "newslet-Digest-abc123")
    settings.cache_clear()

    calls: list[dict] = []

    class _FakeLambda:
        def invoke(self, **kwargs):
            calls.append(kwargs)
            return {"StatusCode": 202}

    monkeypatch.setattr(web.boto3, "client", lambda svc, **_: _FakeLambda())

    client.cookies.set("admin_token", "supersecret")
    r = client.post("/api/send-now")

    assert r.status_code == 303
    assert r.headers["location"] == "/?sent=1"
    assert len(calls) == 1
    assert calls[0]["FunctionName"] == "newslet-Digest-abc123"
    assert calls[0]["InvocationType"] == "Event"  # async, fire-and-forget
    assert json.loads(calls[0]["Payload"]) == {"manual": True}


def test_send_now_503_when_not_configured(client, monkeypatch):
    """Without DIGEST_FUNCTION_NAME the route fails loudly, not with a
    vague boto error."""
    from newslet.config import settings

    monkeypatch.delenv("DIGEST_FUNCTION_NAME", raising=False)
    settings.cache_clear()

    client.cookies.set("admin_token", "supersecret")
    r = client.post("/api/send-now")
    assert r.status_code == 503


def test_admin_index_shows_send_now_button(client):
    client.cookies.set("admin_token", "supersecret")
    r = client.get("/")
    assert r.status_code == 200
    assert 'action="/api/send-now"' in r.text


def test_admin_index_flashes_after_send(client):
    client.cookies.set("admin_token", "supersecret")
    r = client.get("/?sent=1")
    assert r.status_code == 200
    assert "on its way" in r.text.lower()
