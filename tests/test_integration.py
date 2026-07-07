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


def _stub_enrichment(monkeypatch, *, summarize=None, discoveries=None, tune=None):
    """Inject offline fakes for summarize / discovery / tune.

    These three modules each call ``anthropic.Anthropic`` directly; without
    stubbing, the pipeline would try to reach the network (and, because the
    rank tests monkeypatch the shared ``anthropic`` module, they would even
    consume the rank fake). Patching the bound functions keeps each test
    focused on the behaviour it asserts.
    """
    from newslet import discovery as discovery_mod
    from newslet import hn as hn_mod
    from newslet import serendipity as serendipity_mod
    from newslet import summarize as summarize_mod
    from newslet import tune as tune_mod
    from newslet import websearch as websearch_mod
    from newslet import x_grok as x_grok_mod

    summarize = summarize or (lambda picks, **_: ("", ""))
    discoveries = discoveries if discoveries is not None else (lambda *_a, **_k: [])
    tune = tune or (lambda md, fb, **_: md)
    monkeypatch.setattr(summarize_mod, "summarize_issue", summarize)
    monkeypatch.setattr(discovery_mod, "find_discoveries", discoveries)
    monkeypatch.setattr(tune_mod, "tune_profile", tune)
    # The HN source and the web-search block both hit the network in
    # production; stub them to offline empties so the pipeline stays offline.
    monkeypatch.setattr(hn_mod, "fetch_hn_articles", lambda *a, **k: [])
    monkeypatch.setattr(websearch_mod, "search_web", lambda *a, **k: [])
    monkeypatch.setattr(serendipity_mod, "fetch_serendipity", lambda *a, **k: [])
    # The X source reaches xAI in production; stub to an offline empty too.
    monkeypatch.setattr(x_grok_mod, "fetch_x_articles", lambda *a, **k: [])


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

    # Wire offline enrichment fakes and capture that they are invoked.
    from newslet.contracts import Discovery

    enrich_calls = {"summarize": 0, "discovery": 0, "tune": 0}

    def fake_summarize(picks, **_):
        enrich_calls["summarize"] += 1
        return ("Fresh things today", "Two fresh stories worth your time.")

    def fake_discoveries(profile_md, feed_domains, **_):
        enrich_calls["discovery"] += 1
        return [
            Discovery(
                url="https://offsite.example.org/scoop",
                title="An Off-Feed Scoop",
                source="Offsite Wire",
                reason="Matches your taste for fresh things.",
                feed_url="https://offsite.example.org/feed.xml",
            )
        ]

    def fake_tune(md, fb, **_):
        enrich_calls["tune"] += 1
        return md + "\n<!-- tuned -->"

    _stub_enrichment(
        monkeypatch,
        summarize=fake_summarize,
        discoveries=fake_discoveries,
        tune=fake_tune,
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

    # Enrichment was wired in: summarize subject overrides the default,
    # intro renders, and the discovery section appears.
    assert enrich_calls == {"summarize": 1, "discovery": 1, "tune": 1}
    assert sent[0]["subject"] == "Fresh things today"
    assert "Two fresh stories worth your time." in sent[0]["html"]
    assert "An Off-Feed Scoop" in sent[0]["html"]
    assert "https://offsite.example.org/scoop" in sent[0]["html"]

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

    # The discovery url is also marked seen so it is not re-surfaced.
    assert db.is_seen("https://offsite.example.org/scoop")

    # Tuning ran after the send and persisted the new profile markdown.
    assert db.get_profile().markdown.endswith("<!-- tuned -->")


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
    _stub_enrichment(monkeypatch)

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
    _stub_enrichment(monkeypatch)

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
    _stub_enrichment(monkeypatch)
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
    assert payload["from"] == "daily scoop <from@example.com>"
    assert payload["to"] == ["to@example.com"]


def test_handler_sends_even_with_zero_picks(aws, monkeypatch):
    """An empty-candidate day should still produce an email so the user
    notices the pipeline ran (vs silently going dark)."""
    from newslet import db, feeds, hn, websearch, x_grok
    from newslet.handlers import digest

    # No entries at all → empty candidate list → no rank call needed.
    monkeypatch.setattr(
        feeds,
        "feedparser",
        SimpleNamespace(parse=lambda _u: SimpleNamespace(
            bozo=0, bozo_exception=None, feed={"title": "T"}, entries=[]
        )),
    )
    # Stub the HN, X, and web-search network edges so "no candidates" really
    # means none — otherwise a source could fill the pool and force a rank call.
    monkeypatch.setattr(hn, "fetch_hn_articles", lambda *a, **k: [])
    monkeypatch.setattr(websearch, "search_web", lambda *a, **k: [])
    monkeypatch.setattr(x_grok, "fetch_x_articles", lambda *a, **k: [])

    sent: list[dict] = []
    monkeypatch.setattr(digest, "_send_email", lambda s, h: sent.append({"s": s, "h": h}))

    db.add_feed("https://example.com/rss")

    result = digest.handler({}, None)
    assert result["status"] == "sent"
    assert result["picks"] == 0
    assert len(sent) == 1
    assert "0 picks today" in sent[0]["h"]  # the email template's footer


def test_run_digest_drops_already_seen_discovery(env, monkeypatch):
    """A discovery URL already in the seen-store is filtered before sending.

    discovery_fn never consults is_seen itself, so run_digest must apply the
    same seen filter the fetcher uses or web search can resurface a story we
    marked seen on an earlier day.
    """
    from newslet import feeds
    from newslet.contracts import (
        Article,
        Discovery,
        Pick,
        Profile,
        RankResponse,
    )
    from newslet.handlers import digest

    art = Article(
        url="https://example.com/a",
        title="A",
        summary="s",
        source="src",
        published=datetime.now(UTC),
    )
    monkeypatch.setattr(feeds, "fetch_recent", lambda *a, **k: [art])

    seen_url = "https://offsite.example/already-seen"
    fresh_url = "https://offsite.example/brand-new"

    def fake_discovery(profile_md, feed_domains, **_):
        return [
            Discovery(url=seen_url, title="seen", source="S", reason="r",
                      feed_url="https://offsite.example/feed.xml"),
            Discovery(url=fresh_url, title="fresh", source="S", reason="r",
                      feed_url="https://offsite.example/feed.xml"),
        ]

    issue, _candidates = digest.run_digest(
        feed_urls=["https://feed.example/rss"],
        profile=Profile(markdown="p", updated_at=datetime.now(UTC)),
        feedback=[],
        is_seen=lambda u: u == seen_url,
        rank_fn=lambda **k: RankResponse(
            picks=[Pick(url=art.url, title="A", blurb="b", source="src", score=0.5)]
        ),
        summarize_fn=lambda picks, **k: ("subj", "intro"),
        discovery_fn=fake_discovery,
        hn_fn=lambda *a, **k: [],
        websearch_fn=lambda *a, **k: [],
    )

    urls = [str(d.url) for d in issue.discoveries]
    assert fresh_url in urls
    assert seen_url not in urls


def test_run_digest_merges_hn_and_adds_web_block(env, monkeypatch):
    """HN candidates join the ranking pool and the web-search block lands on
    the issue; both are seen-filtered and best-effort."""
    from newslet import feeds
    from newslet.contracts import (
        Article,
        Pick,
        Profile,
        RankResponse,
        WebArticle,
    )
    from newslet.handlers import digest

    rss = Article(url="https://feed.example/rss-1", title="RSS One", summary="s",
                  source="Feed", published=datetime.now(UTC))
    monkeypatch.setattr(feeds, "fetch_recent", lambda *a, **k: [rss])

    hn_art = Article(url="https://news.ycombinator.com/item?id=1", title="HN One",
                     summary="500 points", source="Hacker News",
                     published=datetime.now(UTC))

    seen_candidates: list[list[str]] = []

    def fake_rank(*, candidates, **k):
        seen_candidates.append([str(c.url) for c in candidates])
        # Pick one from each source to prove HN reached the ranker.
        return RankResponse(picks=[
            Pick(url=c.url, title=c.title, blurb="b", source=c.source, score=0.5)
            for c in candidates
        ])

    issue, _ = digest.run_digest(
        feed_urls=["https://feed.example/rss"],
        profile=Profile(markdown="I like systems.", updated_at=datetime.now(UTC)),
        feedback=[],
        is_seen=lambda u: False,
        rank_fn=fake_rank,
        summarize_fn=lambda picks, **k: ("subj", "intro"),
        discovery_fn=lambda *a, **k: [],
        hn_fn=lambda **k: [hn_art],
        websearch_fn=lambda *a, **k: [
            WebArticle(url="https://web.example/1", title="Web One", source="Web"),
        ],
    )

    # HN candidate was merged into the pool the ranker saw.
    assert "https://news.ycombinator.com/item?id=1" in seen_candidates[0]
    assert "https://feed.example/rss-1" in seen_candidates[0]
    # The web block is attached to the issue.
    assert [str(w.url) for w in issue.web_articles] == ["https://web.example/1"]


def test_run_digest_merges_subscribed_newsletters(env, monkeypatch):
    """Newsletter-extracted articles join the ranking pool, best-effort and
    seen-filtered like HN."""
    from newslet import feeds
    from newslet.contracts import Article, Pick, Profile, RankResponse
    from newslet.handlers import digest

    rss = Article(url="https://feed.example/rss-1", title="RSS One", summary="s",
                  source="Feed", published=datetime.now(UTC))
    monkeypatch.setattr(feeds, "fetch_recent", lambda *a, **k: [rss])

    nl_fresh = Article(url="https://news.example/story", title="Newsletter Story",
                       summary="", source="The Daily", published=datetime.now(UTC))
    nl_seen = Article(url="https://news.example/old", title="Old Story", summary="",
                      source="The Daily", published=datetime.now(UTC))

    seen_candidates: list[list[str]] = []

    def fake_rank(*, candidates, **k):
        seen_candidates.append([str(c.url) for c in candidates])
        return RankResponse(picks=[
            Pick(url=c.url, title=c.title, blurb="b", source=c.source, score=0.5)
            for c in candidates
        ])

    issue, _ = digest.run_digest(
        feed_urls=["https://feed.example/rss"],
        profile=Profile(markdown="p", updated_at=datetime.now(UTC)),
        feedback=[],
        is_seen=lambda u: u == "https://news.example/old",
        rank_fn=fake_rank,
        summarize_fn=lambda picks, **k: ("subj", "intro"),
        discovery_fn=lambda *a, **k: [],
        hn_fn=lambda **k: [],
        websearch_fn=lambda *a, **k: [],
        newsletters_fn=lambda since, **k: [nl_fresh, nl_seen],
    )

    pool = seen_candidates[0]
    # The fresh newsletter article reached the ranker; the already-seen one
    # was filtered out before ranking.
    assert "https://news.example/story" in pool
    assert "https://news.example/old" not in pool
    assert "https://feed.example/rss-1" in pool


def test_run_digest_merges_x_posts(env, monkeypatch):
    """X posts join the ranking pool, best-effort and seen-filtered like HN."""
    from newslet import feeds
    from newslet.contracts import Article, Pick, Profile, RankResponse
    from newslet.handlers import digest

    rss = Article(url="https://feed.example/rss-1", title="RSS One", summary="s",
                  source="Feed", published=datetime.now(UTC))
    monkeypatch.setattr(feeds, "fetch_recent", lambda *a, **k: [rss])

    x_fresh = Article(url="https://x.com/a/status/1", title="Fresh Post", summary="",
                      source="X", published=datetime.now(UTC))
    x_seen = Article(url="https://x.com/a/status/2", title="Old Post", summary="",
                     source="X", published=datetime.now(UTC))

    seen_candidates: list[list[str]] = []

    def fake_rank(*, candidates, **k):
        seen_candidates.append([str(c.url) for c in candidates])
        return RankResponse(picks=[
            Pick(url=c.url, title=c.title, blurb="b", source=c.source, score=0.5)
            for c in candidates
        ])

    issue, _ = digest.run_digest(
        feed_urls=["https://feed.example/rss"],
        profile=Profile(markdown="p", updated_at=datetime.now(UTC)),
        feedback=[],
        is_seen=lambda u: u == "https://x.com/a/status/2",
        rank_fn=fake_rank,
        summarize_fn=lambda picks, **k: ("subj", "intro"),
        discovery_fn=lambda *a, **k: [],
        hn_fn=lambda **k: [],
        websearch_fn=lambda *a, **k: [],
        newsletters_fn=lambda since, **k: [],
        x_fn=lambda *a, **k: [x_fresh, x_seen],
    )

    pool = seen_candidates[0]
    # The fresh post reached the ranker; the already-seen one was filtered out.
    assert "https://x.com/a/status/1" in pool
    assert "https://x.com/a/status/2" not in pool
    assert "https://feed.example/rss-1" in pool


def test_run_digest_skips_x_when_disabled(env, monkeypatch):
    """x_enabled=False must not call the X source at all."""
    from newslet import feeds
    from newslet.contracts import Article, Pick, Profile, RankResponse
    from newslet.handlers import digest

    rss = Article(url="https://feed.example/rss-1", title="RSS One", summary="s",
                  source="Feed", published=datetime.now(UTC))
    monkeypatch.setattr(feeds, "fetch_recent", lambda *a, **k: [rss])

    called = {"x": 0}

    def fake_x(*_a, **_k):
        called["x"] += 1
        return [Article(url="https://x.com/a/status/1", title="X", summary="",
                        source="X", published=datetime.now(UTC))]

    pools: list[list[str]] = []

    def fake_rank(*, candidates, **k):
        pools.append([str(c.url) for c in candidates])
        return RankResponse(picks=[
            Pick(url=c.url, title=c.title, blurb="b", source=c.source, score=0.5)
            for c in candidates
        ])

    digest.run_digest(
        feed_urls=["https://feed.example/rss"],
        profile=Profile(markdown="p", updated_at=datetime.now(UTC)),
        feedback=[],
        is_seen=lambda u: False,
        rank_fn=fake_rank,
        summarize_fn=lambda picks, **k: ("s", "i"),
        discovery_fn=lambda *a, **k: [],
        hn_fn=lambda **k: [],
        websearch_fn=lambda *a, **k: [],
        newsletters_fn=lambda since, **k: [],
        x_fn=fake_x,
        x_enabled=False,
    )
    assert called["x"] == 0
    assert pools[0] == ["https://feed.example/rss-1"]  # no X candidate in pool


def test_run_digest_passes_config_counts_and_variety(env, monkeypatch):
    """The configured max_picks / max_web / web_variety reach rank + websearch."""
    from newslet import feeds
    from newslet.contracts import Article, Pick, Profile, RankResponse
    from newslet.handlers import digest

    art = Article(url="https://feed.example/a", title="A", summary="s", source="F",
                  published=datetime.now(UTC))
    monkeypatch.setattr(feeds, "fetch_recent", lambda *a, **k: [art])

    seen = {}

    def fake_rank(*, max_picks, min_picks, **k):
        seen["max_picks"] = max_picks
        seen["min_picks"] = min_picks
        return RankResponse(picks=[Pick(url=art.url, title="A", blurb="b",
                                        source="F", score=0.5)])

    def fake_web(query, *, max_results, variety, model, max_searches, **k):
        seen["max_web"] = max_results
        seen["variety"] = variety
        # A fast model + few rounds (the reliable web_search config), and NO
        # feed-domain exclusion (that is discovery's job).
        seen["model"] = model
        seen["exclude_hosts"] = k.get("exclude_hosts")
        return []

    digest.run_digest(
        feed_urls=["https://feed.example/rss"],
        profile=Profile(markdown="p", updated_at=datetime.now(UTC)),
        feedback=[],
        is_seen=lambda u: False,
        rank_fn=fake_rank,
        summarize_fn=lambda picks, **k: ("s", "i"),
        discovery_fn=lambda *a, **k: [],
        hn_fn=lambda **k: [],
        websearch_fn=fake_web,
        max_picks=7,
        min_picks=4,
        max_web=3,
        web_variety=85,
    )
    assert seen == {
        "max_picks": 7, "min_picks": 4, "max_web": 3, "variety": 85,
        "model": "claude-haiku-4-5-20251001", "exclude_hosts": None,
    }


def test_run_digest_web_block_keeps_feed_domain_results(env, monkeypatch):
    """A web result from a domain the user already follows is still kept —
    the web block is 'from around the web', not new-source discovery."""
    from newslet import feeds
    from newslet.contracts import Article, Pick, Profile, RankResponse, WebArticle
    from newslet.handlers import digest

    art = Article(url="https://feed.example/a", title="A", summary="s", source="F",
                  published=datetime.now(UTC))
    monkeypatch.setattr(feeds, "fetch_recent", lambda *a, **k: [art])

    issue, _ = digest.run_digest(
        feed_urls=["https://feed.example/rss"],  # feed domain = feed.example
        profile=Profile(markdown="p", updated_at=datetime.now(UTC)),
        feedback=[],
        is_seen=lambda u: False,
        rank_fn=lambda **k: RankResponse(
            picks=[Pick(url=art.url, title="A", blurb="b", source="F", score=0.5)]
        ),
        summarize_fn=lambda picks, **k: ("s", "i"),
        discovery_fn=lambda *a, **k: [],
        hn_fn=lambda **k: [],
        websearch_fn=lambda *a, **k: [
            WebArticle(url="https://feed.example/web-piece", title="Same domain",
                       source="F"),
        ],
        max_web=5,
    )
    assert [str(w.url) for w in issue.web_articles] == ["https://feed.example/web-piece"]


def test_run_digest_skips_web_block_when_max_web_zero(env, monkeypatch):
    """max_web == 0 disables the web block without calling the search."""
    from newslet import feeds
    from newslet.contracts import Article, Pick, Profile, RankResponse
    from newslet.handlers import digest

    art = Article(url="https://feed.example/a", title="A", summary="s", source="F",
                  published=datetime.now(UTC))
    monkeypatch.setattr(feeds, "fetch_recent", lambda *a, **k: [art])

    called = {"web": 0}

    def fake_web(*a, **k):
        called["web"] += 1
        return []

    issue, _ = digest.run_digest(
        feed_urls=["https://feed.example/rss"],
        profile=Profile(markdown="p", updated_at=datetime.now(UTC)),
        feedback=[],
        is_seen=lambda u: False,
        rank_fn=lambda **k: RankResponse(
            picks=[Pick(url=art.url, title="A", blurb="b", source="F", score=0.5)]
        ),
        summarize_fn=lambda picks, **k: ("s", "i"),
        discovery_fn=lambda *a, **k: [],
        hn_fn=lambda **k: [],
        websearch_fn=fake_web,
        max_web=0,
    )
    assert called["web"] == 0
    assert issue.web_articles == []


def test_home_mode_stores_home_issue_without_emailing(aws, monkeypatch):
    """handler({"home": True}) builds the homepage aggregation under the
    reserved 'home' key, hidden from list_issues, and sends no email."""
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
             "blurb": "b", "source": "Test Feed", "score": 0.9},
        ]})),
    )
    _stub_enrichment(monkeypatch)

    sent: list = []
    monkeypatch.setattr(digest, "_send_email", lambda s, h: sent.append((s, h)))

    db.add_feed("https://example.com/rss")
    db.put_profile("I like fresh things.")

    result = digest.handler({"home": True}, None)

    assert result["status"] == "home_refreshed"
    assert sent == []  # the homepage never emails
    home = db.get_issue("home")
    assert home is not None
    assert len(home.picks) == 1
    # Hidden from the daily "recent issues" list.
    assert "home" not in [i["date"] for i in db.list_issues()]


def test_run_digest_drops_seen_web_article(env, monkeypatch):
    """A web-search result already in the seen-store is filtered, like
    discoveries."""
    from newslet import feeds
    from newslet.contracts import Article, Pick, Profile, RankResponse, WebArticle
    from newslet.handlers import digest

    art = Article(url="https://feed.example/a", title="A", summary="s", source="F",
                  published=datetime.now(UTC))
    monkeypatch.setattr(feeds, "fetch_recent", lambda *a, **k: [art])

    seen = "https://web.example/seen"
    fresh = "https://web.example/fresh"

    issue, _ = digest.run_digest(
        feed_urls=["https://feed.example/rss"],
        profile=Profile(markdown="p", updated_at=datetime.now(UTC)),
        feedback=[],
        is_seen=lambda u: u == seen,
        rank_fn=lambda **k: RankResponse(
            picks=[Pick(url=art.url, title="A", blurb="b", source="F", score=0.5)]
        ),
        summarize_fn=lambda picks, **k: ("s", "i"),
        discovery_fn=lambda *a, **k: [],
        hn_fn=lambda **k: [],
        websearch_fn=lambda *a, **k: [
            WebArticle(url=seen, title="seen", source="W"),
            WebArticle(url=fresh, title="fresh", source="W"),
        ],
    )
    urls = [str(w.url) for w in issue.web_articles]
    assert fresh in urls
    assert seen not in urls


def test_manual_run_sends_but_stays_invisible_to_cadence(aws, monkeypatch):
    """A {"manual": true} run sends a real email with a working feedback
    loop, but does not touch the daily cadence: it ignores the
    already-sent gate, uses a synthetic issue key hidden from
    list_issues, and never marks candidates seen — yet still re-tunes."""
    from newslet import db, feeds, rank
    from newslet.contracts import Issue
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
             "blurb": "b", "source": "Test Feed", "score": 0.9},
        ]})),
    )
    _stub_enrichment(monkeypatch, tune=lambda md, fb, **_: md + "\n<!-- tuned -->")

    sent: list[dict] = []
    monkeypatch.setattr(
        digest, "_send_email", lambda s, h: sent.append({"subject": s, "html": h})
    )

    db.add_feed("https://example.com/rss")
    db.put_profile("I like fresh things.")

    # Today's real issue has already been sent — the daily gate is closed.
    today = now.strftime("%Y-%m-%d")
    db.put_issue(Issue(date=today, picks=[], created_at=now))
    db.mark_issue_sent(today)

    result = digest.handler({"manual": True}, None)

    # It sent, despite today already being sent (no idempotency gate).
    assert result["status"] == "sent"
    assert len(sent) == 1
    assert result["date"].startswith("manual-")
    assert "Fresh One" in sent[0]["html"]

    # Rate links carry the synthetic key, so the feedback loop is live.
    assert f"d={result['date']}" in sent[0]["html"]

    # Invisible to "recent issues": only today's real row is listed,
    # but the manual issue is still directly retrievable.
    assert [i["date"] for i in db.list_issues()] == [today]
    assert db.get_issue(result["date"]) is not None

    # Did NOT count toward timing: today's marker is untouched (still the
    # original send), and candidates were NOT marked seen.
    assert not db.is_seen("https://example.com/fresh-1")
    assert not db.is_seen("https://example.com/fresh-2")

    # ...but tuning still ran, faithful to a real run.
    assert db.get_profile().markdown.endswith("<!-- tuned -->")


def test_concurrent_manual_runs_get_distinct_issue_keys(aws, monkeypatch):
    """Two manual sends fired in the same instant (e.g. a double-click or a
    browser POST retry) must persist as distinct issue rows. A
    second-precision key would collide and the later put_issue would
    overwrite the earlier issue's picks."""
    from datetime import datetime as real_datetime

    from newslet import db, feeds, rank
    from newslet.handlers import digest

    # Freeze the clock so both runs share the same wall-clock instant —
    # the worst case the synthetic key must survive.
    frozen = real_datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)

    class _FrozenDatetime:
        @staticmethod
        def now(tz=None):
            return frozen

    monkeypatch.setattr(digest, "datetime", _FrozenDatetime)
    monkeypatch.setattr(
        feeds,
        "feedparser",
        SimpleNamespace(parse=lambda _u: _build_feedparser_fixture(frozen)),
    )
    monkeypatch.setattr(
        rank.anthropic,
        "Anthropic",
        lambda **_: _FakeAnthropic(json.dumps({"picks": [
            {"url": "https://example.com/fresh-1", "title": "Fresh One",
             "blurb": "b", "source": "Test Feed", "score": 0.9},
        ]})),
    )
    _stub_enrichment(monkeypatch)
    monkeypatch.setattr(digest, "_send_email", lambda s, h: None)

    db.add_feed("https://example.com/rss")

    first = digest.handler({"manual": True}, None)
    second = digest.handler({"manual": True}, None)

    # Distinct keys despite the identical timestamp...
    assert first["date"] != second["date"]
    # ...and both issues persist independently (no overwrite).
    assert db.get_issue(first["date"]) is not None
    assert db.get_issue(second["date"]) is not None
