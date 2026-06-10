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
    monkeypatch.setenv("MAIL_DOMAIN", "inbox.example.com")
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
        ddb.create_table(
            TableName="newslet-subscriptions",
            KeySchema=[{"AttributeName": "address", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "address", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


@pytest.fixture
def client(aws):
    from newslet.handlers.web import app

    # follow_redirects=False so we can inspect the 303s.
    return TestClient(app, follow_redirects=False)


# ---------------------------------------------------------------------------
# Canonical host (www -> apex)
# ---------------------------------------------------------------------------


def test_www_host_redirects_to_apex(client):
    """Requests to www.<domain> get a 301 to the bare apex over https,
    preserving path and query so links and bookmarks survive."""
    r = client.get("/emails?page=2", headers={"host": "www.dailyscoop.email"})
    assert r.status_code == 301
    assert r.headers["location"] == "https://dailyscoop.email/emails?page=2"


def test_apex_host_is_served_directly(client):
    """The bare apex is served normally — no redirect (guards against a loop)."""
    r = client.get("/login", headers={"host": "dailyscoop.email"})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Product guide (public docs)
# ---------------------------------------------------------------------------


def test_docs_viewer_is_public_html(client):
    """The product guide renders without an admin cookie and points the viewer
    at the markdown source."""
    r = client.get("/docs")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "product guide" in r.text.lower()
    # The viewer pulls the markdown in real time from this route.
    assert "/docs/content.md" in r.text


def test_docs_markdown_is_public_and_tiered(client):
    """The canonical markdown is served as markdown and carries the
    complexity-tier fences the viewer filters on."""
    r = client.get("/docs/content.md")
    assert r.status_code == 200
    assert "text/markdown" in r.headers["content-type"]
    assert "# daily scoop" in r.text
    # The three depth levels are encoded as :::tier fences.
    assert ":::tier little" in r.text
    assert ":::tier medium" in r.text


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
    assert r.headers["location"] == "/admin"

    r = client.get("/admin")
    assert r.status_code == 200
    # Look for the delete form which only renders for actual feed rows
    assert 'value="https://example.com/rss"' in r.text

    r = client.post("/api/feeds/delete", data={"url": "https://example.com/rss"})
    assert r.status_code == 303
    r = client.get("/admin")
    assert 'value="https://example.com/rss"' not in r.text


def test_profile_save(client):
    client.cookies.set("admin_token", "supersecret")
    r = client.post("/api/profile", data={"markdown": "I like LLMs and Postgres."})
    assert r.status_code == 303
    r = client.get("/admin")
    assert "I like LLMs and Postgres." in r.text


def test_subscription_create_and_delete_roundtrip(client):
    client.cookies.set("admin_token", "supersecret")
    r = client.post("/api/subscriptions", data={"source": "Stratechery"})
    assert r.status_code == 303
    assert r.headers["location"] == "/admin"

    r = client.get("/admin")
    assert r.status_code == 200
    assert "Stratechery" in r.text
    assert "@inbox.example.com" in r.text  # a generated address is shown

    # Pull the generated address out of the DB to delete it.
    from newslet import db

    subs = db.list_subscriptions()
    assert len(subs) == 1
    addr = subs[0].address

    r = client.post("/api/subscriptions/delete", data={"address": addr})
    assert r.status_code == 303
    assert db.list_subscriptions() == []


def test_subscription_create_requires_mail_domain(client, monkeypatch):
    from newslet.config import settings

    monkeypatch.setenv("MAIL_DOMAIN", "")
    settings.cache_clear()
    client.cookies.set("admin_token", "supersecret")
    r = client.post("/api/subscriptions", data={"source": "X"})
    assert r.status_code == 503


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

    r = client.get("/admin")
    assert 'value="https://example.com/Rss"' in r.text

    r = client.post("/api/feeds/delete", data={"url": "https://EXAMPLE.com/Rss"})
    assert r.status_code == 303

    r = client.get("/admin")
    assert 'value="https://example.com/Rss"' not in r.text


def test_emails_index_lists_recent_emails(client):
    from datetime import UTC, datetime

    from newslet import db
    from newslet.contracts import Issue

    client.cookies.set("admin_token", "supersecret")

    db.put_issue(Issue(date="2026-05-17", picks=[], created_at=datetime.now(UTC)))
    db.put_issue(Issue(date="2026-05-16", picks=[], created_at=datetime.now(UTC)))
    db.mark_issue_sent("2026-05-16")

    r = client.get("/emails")
    assert r.status_code == 200
    assert "Recent emails" in r.text
    assert "2026-05-17" in r.text
    assert "2026-05-16" in r.text
    # The 2026-05-16 email should show as sent; 2026-05-17 as unsent
    assert "sent" in r.text
    assert "unsent" in r.text
    # Links point at the renamed archive route.
    assert "/emails/2026-05-17" in r.text


def test_admin_index_shows_last_sent(client):
    from datetime import UTC, datetime

    from newslet import db
    from newslet.contracts import Issue

    client.cookies.set("admin_token", "supersecret")
    db.put_issue(Issue(date="2026-05-15", picks=[], created_at=datetime.now(UTC)))
    db.mark_issue_sent("2026-05-15")

    r = client.get("/admin")
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
    assert r.headers["location"] == "/admin?sent=1"
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
    r = client.get("/admin")
    assert r.status_code == 200
    assert 'action="/api/send-now"' in r.text


def test_admin_index_flashes_after_send(client):
    client.cookies.set("admin_token", "supersecret")
    r = client.get("/admin?sent=1")
    assert r.status_code == 200
    assert "on its way" in r.text.lower()


# ---------------------------------------------------------------------------
# Rich homepage + issue archive (now separate surfaces)
# ---------------------------------------------------------------------------


def _seed_issue(key):
    """Seed an issue (picks + web articles) under ``key`` (a date or 'home')."""
    from datetime import UTC, datetime

    from newslet import db
    from newslet.contracts import Issue, Pick, WebArticle

    db.put_issue(
        Issue(
            date=key,
            picks=[
                Pick(url="https://ex.com/a", title="Alpha Pick", blurb="ab",
                     source="Test Feed", score=0.9),
                Pick(url="https://ex.com/b", title="Beta Pick", blurb="bb",
                     source="Hacker News", score=0.4),
            ],
            created_at=datetime.now(UTC),
            subject="Sharp subject",
            intro="An intro line.",
            web_articles=[
                WebArticle(url="https://ex.com/w", title="Web One", blurb="wb",
                           source="Open Web"),
                WebArticle(url="https://news.ycombinator.com/item?id=9",
                           title="HN Rich", blurb="hb", source="Hacker News",
                           points=222, comments=33,
                           comments_url="https://news.ycombinator.com/item?id=9"),
            ],
        )
    )
    return key


def test_homepage_renders_all_articles(client):
    from datetime import UTC, datetime

    client.cookies.set("admin_token", "supersecret")
    _seed_issue("home")
    r = client.get("/")
    assert r.status_code == 200
    for title in ["Alpha Pick", "Beta Pick", "Web One", "HN Rich"]:
        assert title in r.text
    assert "Sharp subject" in r.text
    assert "An intro line." in r.text
    assert "222" in r.text  # HN points badge
    assert 'action="/api/vote"' in r.text
    # The research/search form lives at the bottom, after the article grids.
    assert r.text.index('id="search-form"') > r.text.index('id="web-grid"')
    # No refresh button, no source filter, non-sticky header.
    assert 'id="refresh-btn"' not in r.text
    assert "data-filter" not in r.text
    assert "position: sticky" not in r.text
    # The date header carries today's written weekday.
    assert datetime.now(UTC).strftime("%A") in r.text


def test_homepage_downvoted_article_disappears(client):
    from datetime import UTC, datetime

    from newslet import db
    from newslet.contracts import FeedbackRow

    client.cookies.set("admin_token", "supersecret")
    _seed_issue("home")
    db.put_feedback(
        FeedbackRow(article_url="https://ex.com/b", title="Beta Pick",
                    rating="down", ts=datetime.now(UTC), issue_date="home")
    )
    r = client.get("/")
    assert r.status_code == 200
    # The downvoted article is gone; the others remain.
    assert "Beta Pick" not in r.text
    assert "Alpha Pick" in r.text


def test_homepage_empty_state_auto_prepares(client):
    client.cookies.set("admin_token", "supersecret")
    r = client.get("/")
    assert r.status_code == 200
    # No stored edition yet → the page prepares one itself (no manual button).
    assert "Preparing today's edition" in r.text
    assert 'id="refresh-btn"' not in r.text


def test_homepage_shows_sticky_vote_state(client):
    from datetime import UTC, datetime

    from newslet import db
    from newslet.contracts import FeedbackRow

    client.cookies.set("admin_token", "supersecret")
    _seed_issue("home")
    db.put_feedback(
        FeedbackRow(
            article_url="https://ex.com/a",
            title="Alpha Pick",
            rating="up",
            ts=datetime.now(UTC),
            issue_date="home",
        )
    )
    r = client.get("/")
    assert r.status_code == 200
    assert "voted-up" in r.text


def test_homepage_requires_admin(client):
    r = client.get("/")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_homepage_server_rendered_search(client, monkeypatch):
    from newslet.contracts import WebArticle
    from newslet.handlers import web

    client.cookies.set("admin_token", "supersecret")
    _seed_issue("home")

    monkeypatch.setattr(
        web.websearch,
        "search_web",
        lambda q, **k: [
            WebArticle(url="https://ex.com/found", title="Found Article",
                       blurb="from search", source="Search Src")
        ],
    )
    r = client.get("/", params={"q": "neural nets"})
    assert r.status_code == 200
    assert "Found Article" in r.text
    assert "neural nets" in r.text


def test_email_archive_renders_email(client):
    """/emails/{date} shows the as-sent email (separate from the homepage)."""
    client.cookies.set("admin_token", "supersecret")
    _seed_issue("2026-05-21")
    r = client.get("/emails/2026-05-21")
    assert r.status_code == 200
    assert "Alpha Pick" in r.text
    assert "picks today" in r.text  # email footer


def test_email_view_404_for_missing(client):
    client.cookies.set("admin_token", "supersecret")
    r = client.get("/emails/2099-01-01")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Vote / search / HN endpoints
# ---------------------------------------------------------------------------


def test_vote_requires_admin(client):
    r = client.post("/api/vote", data={"url": "https://ex.com/a", "rating": "up",
                                       "date": "2026-05-21"})
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_vote_records_feedback_and_redirects(client):
    from newslet import db

    client.cookies.set("admin_token", "supersecret")
    r = client.post(
        "/api/vote",
        data={"url": "https://ex.com/a", "title": "Alpha", "rating": "up",
              "date": "2026-05-21"},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    ratings = db.feedback_ratings(["https://ex.com/a"], "2026-05-21")
    assert ratings == {"https://ex.com/a": "up"}


def test_vote_returns_json_when_accept_json(client):
    client.cookies.set("admin_token", "supersecret")
    r = client.post(
        "/api/vote",
        data={"url": "https://ex.com/a", "title": "Alpha", "rating": "down",
              "date": "2026-05-21"},
        headers={"accept": "application/json"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["rating"] == "down"


def test_vote_rejects_bad_rating(client):
    client.cookies.set("admin_token", "supersecret")
    r = client.post(
        "/api/vote",
        data={"url": "https://ex.com/a", "rating": "sideways", "date": "2026-05-21"},
    )
    assert r.status_code == 400


def test_vote_rejects_bad_url(client):
    client.cookies.set("admin_token", "supersecret")
    r = client.post(
        "/api/vote",
        data={"url": "not-a-url", "rating": "up", "date": "2026-05-21"},
    )
    assert r.status_code == 400


def test_api_search_returns_json(client, monkeypatch):
    from newslet.contracts import WebArticle
    from newslet.handlers import web

    client.cookies.set("admin_token", "supersecret")
    monkeypatch.setattr(
        web.websearch,
        "search_web",
        lambda q, **k: [
            WebArticle(url="https://ex.com/r1", title="Result 1", blurb="b",
                       source="Src"),
        ],
    )
    r = client.get("/api/search", params={"q": "rust async"})
    assert r.status_code == 200
    data = r.json()
    assert data["query"] == "rust async"
    assert data["results"][0]["url"] == "https://ex.com/r1"


def test_api_search_requires_admin(client):
    r = client.get("/api/search", params={"q": "x"})
    assert r.status_code == 303


def test_api_hn_returns_json(client, monkeypatch):
    from newslet.contracts import WebArticle
    from newslet.handlers import web

    client.cookies.set("admin_token", "supersecret")
    monkeypatch.setattr(
        web.hn,
        "fetch_hn_rich",
        lambda **k: [
            WebArticle(url="https://news.ycombinator.com/item?id=1", title="HN 1",
                       blurb="", source="Hacker News", points=10, comments=2,
                       comments_url="https://news.ycombinator.com/item?id=1"),
        ],
    )
    r = client.get("/api/hn")
    assert r.status_code == 200
    data = r.json()
    assert data["results"][0]["points"] == 10
    assert data["results"][0]["source"] == "Hacker News"


# ---------------------------------------------------------------------------
# Admin config
# ---------------------------------------------------------------------------


def test_config_save_and_render(client):
    from newslet import db

    client.cookies.set("admin_token", "supersecret")
    r = client.post(
        "/api/config",
        data={
            "max_rss_articles": "15",
            "max_web_articles": "8",
            "web_variety": "70",
            "x_enabled": "true",
            "max_x_articles": "12",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin"

    cfg = db.get_config()
    assert cfg.max_rss_articles == 15
    assert cfg.max_web_articles == 8
    assert cfg.web_variety == 70
    assert cfg.x_enabled is True
    assert cfg.max_x_articles == 12

    # The admin page shows the saved values.
    r = client.get("/admin")
    assert 'name="max_rss_articles"' in r.text
    assert 'value="15"' in r.text
    assert 'name="web_variety"' in r.text
    assert 'name="x_enabled"' in r.text
    assert 'name="max_x_articles"' in r.text


def test_config_x_disabled_when_checkbox_absent(client):
    """An unchecked X checkbox submits nothing, which must persist as off."""
    from newslet import db

    client.cookies.set("admin_token", "supersecret")
    r = client.post(
        "/api/config",
        # No x_enabled key — mirrors an unchecked checkbox.
        data={"max_rss_articles": "10", "max_web_articles": "5", "web_variety": "30"},
    )
    assert r.status_code == 303
    assert db.get_config().x_enabled is False


def test_config_rejects_out_of_range(client):
    client.cookies.set("admin_token", "supersecret")
    r = client.post(
        "/api/config",
        data={"max_rss_articles": "999", "max_web_articles": "1", "web_variety": "10"},
    )
    assert r.status_code == 400


def test_config_requires_admin(client):
    r = client.post(
        "/api/config",
        data={"max_rss_articles": "10", "max_web_articles": "5", "web_variety": "30"},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# Homepage refresh
# ---------------------------------------------------------------------------


def test_home_refresh_invokes_digest_home_mode(client, monkeypatch):
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
    r = client.post("/api/home/refresh", headers={"accept": "application/json"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert len(calls) == 1
    assert calls[0]["InvocationType"] == "Event"
    assert json.loads(calls[0]["Payload"]) == {"home": True}


def test_home_refresh_requires_admin(client):
    r = client.post("/api/home/refresh")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_home_status_reports_freshness(client):
    from datetime import UTC, datetime

    from newslet import db
    from newslet.contracts import Issue

    client.cookies.set("admin_token", "supersecret")

    # No home doc yet.
    r = client.get("/api/home/status")
    assert r.status_code == 200
    assert r.json()["ready"] is False

    db.put_issue(Issue(date="home", picks=[], created_at=datetime.now(UTC)), manual=True)
    r = client.get("/api/home/status")
    assert r.json()["ready"] is True
    assert r.json()["created_at"]


# ---------------------------------------------------------------------------
# Themes
# ---------------------------------------------------------------------------


def _save_config(client, **overrides):
    data = {
        "max_rss_articles": "10",
        "max_web_articles": "5",
        "web_variety": "30",
        "x_enabled": "true",
        "max_x_articles": "15",
    }
    data.update(overrides)
    return client.post("/api/config", data=data)


def test_config_save_persists_theme(client):
    from newslet import db

    client.cookies.set("admin_token", "supersecret")
    r = _save_config(client, theme="phosphor")
    assert r.status_code == 303
    assert db.get_config().theme == "phosphor"

    # The admin picker shows the saved theme selected.
    r = client.get("/admin")
    assert 'name="theme"' in r.text
    assert 'value="phosphor" selected' in r.text


def test_config_theme_defaults_to_classic_when_absent(client):
    """A pre-themes client posting only the original fields keeps classic."""
    from newslet import db

    client.cookies.set("admin_token", "supersecret")
    r = client.post(
        "/api/config",
        data={"max_rss_articles": "10", "max_web_articles": "5", "web_variety": "30"},
    )
    assert r.status_code == 303
    assert db.get_config().theme == "classic"


def test_config_rejects_unknown_theme(client):
    client.cookies.set("admin_token", "supersecret")
    r = _save_config(client, theme="vaporwave")
    assert r.status_code == 400


def test_pages_render_selected_theme(client):
    from newslet import themes

    client.cookies.set("admin_token", "supersecret")
    _save_config(client, theme="amber")
    amber_bg = themes.THEMES["amber"].palette.bg
    for path in ("/", "/admin", "/emails", "/login"):
        r = client.get(path)
        assert r.status_code == 200
        assert f"--bg: {amber_bg};" in r.text, path


def test_default_pages_render_classic_theme(client):
    from newslet import themes

    client.cookies.set("admin_token", "supersecret")
    r = client.get("/admin")
    assert f"--bg: {themes.THEMES['classic'].palette.bg};" in r.text
    # Classic keeps its automatic dark-mode variant.
    assert "@media (prefers-color-scheme: dark)" in r.text


def test_unknown_stored_theme_falls_back_to_classic(client):
    """A stored theme name this build doesn't know must not break pages."""
    import boto3

    from newslet import themes

    client.cookies.set("admin_token", "supersecret")
    boto3.resource("dynamodb", region_name="us-east-1").Table(
        "newslet-profile"
    ).put_item(Item={"id": "config", "theme": "from-the-future"})

    r = client.get("/")
    assert r.status_code == 200
    assert f"--bg: {themes.THEMES['classic'].palette.bg};" in r.text


def test_email_archive_view_uses_selected_theme(client):
    from newslet import themes

    client.cookies.set("admin_token", "supersecret")
    _save_config(client, theme="dos")
    _seed_issue("2026-05-22")
    r = client.get("/emails/2026-05-22")
    assert r.status_code == 200
    assert f"background:{themes.THEMES['dos'].palette.bg}" in r.text


def test_login_page_survives_config_read_failure(client, monkeypatch):
    """Login must stay reachable even if the theme lookup blows up."""
    from newslet import db
    from newslet.handlers import web as web_handler

    def boom():
        raise RuntimeError("dynamo down")

    monkeypatch.setattr(db, "get_config", boom)
    assert web_handler.db.get_config is boom
    r = client.get("/login")
    assert r.status_code == 200
    assert "Admin token" in r.text
