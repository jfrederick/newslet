"""Unit tests for :mod:`newslet.handlers.digest` — covering exception paths,
deduplication, handler routing, CLI helpers, and the daily idempotency logic.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import boto3
import moto
import pytest

from newslet.config import settings
from newslet.contracts import (
    Article,
    Issue,
    Pick,
    Profile,
    RankResponse,
)


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

        def _hash_table(name, key):
            ddb.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[
                    {"AttributeName": key, "AttributeType": "S"}
                ],
                BillingMode="PAY_PER_REQUEST",
            )

        _hash_table("newslet-feeds", "url")
        _hash_table("newslet-profile", "id")
        _hash_table("newslet-seen-articles", "url_hash")
        _hash_table("newslet-issues", "date")
        _hash_table("newslet-subscriptions", "address")

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
            GlobalSecondaryIndexes=[{
                "IndexName": "feedback-by-ts",
                "KeySchema": [
                    {"AttributeName": "bucket", "KeyType": "HASH"},
                    {"AttributeName": "ts", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName="newslet-inbox",
            KeySchema=[
                {"AttributeName": "message_id", "KeyType": "HASH"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "message_id", "AttributeType": "S"},
                {"AttributeName": "bucket", "AttributeType": "S"},
                {"AttributeName": "received_at", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "inbox-by-ts",
                "KeySchema": [
                    {"AttributeName": "bucket", "KeyType": "HASH"},
                    {"AttributeName": "received_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


# --- Helper factories ---


def _article(url: str, title: str = "T") -> Article:
    return Article(url=url, title=title, summary="s", source="Src", published=datetime.now(UTC))


def _pick(url: str, title: str = "T") -> Pick:
    return Pick(url=url, title=title, blurb="b", source="Src", score=0.9)


def _rank_response(picks: list[Pick]) -> RankResponse:
    return RankResponse(picks=picks)


# --- _dedupe_candidates ---


def test_dedupe_candidates_removes_duplicates():
    from newslet.handlers.digest import _dedupe_candidates

    a1 = _article("https://a.example.com/1", "First")
    a2 = _article("https://a.example.com/1", "Duplicate")
    a3 = _article("https://b.example.com/2", "Second")
    result = _dedupe_candidates([a1, a2, a3])
    assert len(result) == 2
    assert result[0].title == "First"
    assert result[1].title == "Second"


# --- _feed_domains ---


def test_feed_domains_extracts_netlocs():
    from newslet.handlers.digest import _feed_domains

    urls = ["https://example.com/feed", "https://blog.io/rss", "not-a-url"]
    result = _feed_domains(urls)
    assert "example.com" in result
    assert "blog.io" in result
    # "not-a-url" has no netloc
    assert len(result) == 2


# --- _web_search_query ---


def test_web_search_query_uses_profile():
    from newslet.handlers.digest import _web_search_query

    result = _web_search_query("I love ML and robotics")
    assert "ML and robotics" in result


def test_web_search_query_defaults_when_empty():
    from newslet.handlers.digest import _web_search_query

    result = _web_search_query("")
    assert "technology" in result


# --- run_digest exception paths ---


def test_run_digest_hn_exception_is_swallowed(env):
    from newslet.handlers.digest import run_digest

    def boom_hn(**_):
        raise RuntimeError("HN down")

    issue, candidates = run_digest(
        feed_urls=[],
        profile=Profile(markdown="test", updated_at=datetime.now(UTC)),
        feedback=[],
        is_seen=lambda _: False,
        rank_fn=lambda **_: _rank_response([_pick("https://a.example.com/1")]),
        summarize_fn=lambda *_a, **_k: ("subj", "intro"),
        discovery_fn=lambda *_a, **_k: [],
        hn_fn=boom_hn,
        websearch_fn=lambda *_a, **_k: [],
        newsletters_fn=lambda _s: [],
    )
    # Despite HN failure, we get a valid issue
    assert issue.picks == []  # no candidates means no picks from rank


def test_run_digest_summarize_exception_is_swallowed(env):
    from newslet.handlers.digest import run_digest

    def boom_summarize(*_a, **_k):
        raise RuntimeError("API down")

    issue, _ = run_digest(
        feed_urls=[],
        profile=Profile(markdown="test", updated_at=datetime.now(UTC)),
        feedback=[],
        is_seen=lambda _: False,
        rank_fn=lambda **_: _rank_response([_pick("https://a.example.com/1")]),
        summarize_fn=boom_summarize,
        discovery_fn=lambda *_a, **_k: [],
        hn_fn=lambda **_: [_article("https://hn.example.com/x")],
        websearch_fn=lambda *_a, **_k: [],
        newsletters_fn=lambda _s: [],
    )
    # Issue built despite summarize failure
    assert issue.subject == ""
    assert issue.intro == ""


def test_run_digest_discovery_exception_is_swallowed(env):
    from newslet.handlers.digest import run_digest

    def boom_discovery(*_a, **_k):
        raise RuntimeError("discovery exploded")

    issue, _ = run_digest(
        feed_urls=[],
        profile=Profile(markdown="test", updated_at=datetime.now(UTC)),
        feedback=[],
        is_seen=lambda _: False,
        rank_fn=lambda **_: _rank_response([_pick("https://a.example.com/1")]),
        summarize_fn=lambda *_a, **_k: ("s", "i"),
        discovery_fn=boom_discovery,
        hn_fn=lambda **_: [_article("https://hn.example.com/x")],
        websearch_fn=lambda *_a, **_k: [],
        newsletters_fn=lambda _s: [],
    )
    assert issue.discoveries == []


def test_run_digest_websearch_exception_is_swallowed(env):
    from newslet.handlers.digest import run_digest

    def boom_web(*_a, **_k):
        raise RuntimeError("web search broke")

    issue, _ = run_digest(
        feed_urls=[],
        profile=Profile(markdown="test", updated_at=datetime.now(UTC)),
        feedback=[],
        is_seen=lambda _: False,
        rank_fn=lambda **_: _rank_response([_pick("https://a.example.com/1")]),
        summarize_fn=lambda *_a, **_k: ("s", "i"),
        discovery_fn=lambda *_a, **_k: [],
        hn_fn=lambda **_: [_article("https://hn.example.com/x")],
        websearch_fn=boom_web,
        newsletters_fn=lambda _s: [],
        max_web=5,
    )
    assert issue.web_articles == []


def test_run_digest_newsletters_exception_is_swallowed(env):
    from newslet.handlers.digest import run_digest

    def boom_nl(_since):
        raise RuntimeError("newsletters broken")

    issue, _ = run_digest(
        feed_urls=[],
        profile=Profile(markdown="test", updated_at=datetime.now(UTC)),
        feedback=[],
        is_seen=lambda _: False,
        rank_fn=lambda **_: _rank_response([_pick("https://a.example.com/1")]),
        summarize_fn=lambda *_a, **_k: ("s", "i"),
        discovery_fn=lambda *_a, **_k: [],
        hn_fn=lambda **_: [_article("https://hn.example.com/x")],
        websearch_fn=lambda *_a, **_k: [],
        newsletters_fn=boom_nl,
    )
    # Still produces a valid issue
    assert isinstance(issue, Issue)


def test_run_digest_no_candidates_returns_empty_issue(env):
    from newslet.handlers.digest import run_digest

    issue, candidates = run_digest(
        feed_urls=[],
        profile=Profile(markdown="test", updated_at=datetime.now(UTC)),
        feedback=[],
        is_seen=lambda _: False,
        rank_fn=lambda **_: _rank_response([]),
        hn_fn=lambda **_: [],
        newsletters_fn=lambda _s: [],
    )
    assert issue.picks == []
    assert candidates == []


def test_run_digest_serendipity_returns_random_articles(env):
    from newslet.contracts import WebArticle
    from newslet.handlers.digest import run_digest

    random_articles = [
        WebArticle(url="https://off.example.com/1", title="Off One", blurb="b", source="Src"),
        WebArticle(url="https://off.example.com/2", title="Off Two", blurb="b", source="Src"),
    ]

    issue, _ = run_digest(
        feed_urls=[],
        profile=Profile(markdown="test", updated_at=datetime.now(UTC)),
        feedback=[],
        is_seen=lambda _: False,
        rank_fn=lambda **_: _rank_response([_pick("https://a.example.com/1")]),
        summarize_fn=lambda *_a, **_k: ("s", "i"),
        discovery_fn=lambda *_a, **_k: [],
        hn_fn=lambda **_: [_article("https://hn.example.com/x")],
        websearch_fn=lambda *_a, **_k: [],
        newsletters_fn=lambda _s: [],
        serendipity_fn=lambda *_a, **_k: random_articles,
    )
    assert issue.random_articles == random_articles


def test_run_digest_serendipity_exception_is_swallowed(env):
    from newslet.handlers.digest import run_digest

    def boom_serendipity(*_a, **_k):
        raise RuntimeError("serendipity exploded")

    issue, _ = run_digest(
        feed_urls=[],
        profile=Profile(markdown="test", updated_at=datetime.now(UTC)),
        feedback=[],
        is_seen=lambda _: False,
        rank_fn=lambda **_: _rank_response([_pick("https://a.example.com/1")]),
        summarize_fn=lambda *_a, **_k: ("s", "i"),
        discovery_fn=lambda *_a, **_k: [],
        hn_fn=lambda **_: [_article("https://hn.example.com/x")],
        websearch_fn=lambda *_a, **_k: [],
        newsletters_fn=lambda _s: [],
        serendipity_fn=boom_serendipity,
    )
    # Issue built despite serendipity failure
    assert issue.random_articles == []


def test_run_digest_max_random_zero_skips_serendipity(env):
    from newslet.handlers.digest import run_digest

    def boom_if_called(*_a, **_k):
        raise AssertionError("serendipity_fn should not be called when max_random=0")

    issue, _ = run_digest(
        feed_urls=[],
        profile=Profile(markdown="test", updated_at=datetime.now(UTC)),
        feedback=[],
        is_seen=lambda _: False,
        rank_fn=lambda **_: _rank_response([_pick("https://a.example.com/1")]),
        summarize_fn=lambda *_a, **_k: ("s", "i"),
        discovery_fn=lambda *_a, **_k: [],
        hn_fn=lambda **_: [_article("https://hn.example.com/x")],
        websearch_fn=lambda *_a, **_k: [],
        newsletters_fn=lambda _s: [],
        serendipity_fn=boom_if_called,
        max_random=0,
    )
    assert issue.random_articles == []


def test_run_digest_serendipity_seen_result_is_dropped(env):
    from newslet.contracts import WebArticle
    from newslet.handlers.digest import run_digest

    seen_url = "https://off.example.com/seen"
    random_articles = [
        WebArticle(url=seen_url, title="Seen One", blurb="b", source="Src"),
        WebArticle(url="https://off.example.com/fresh", title="Fresh One", blurb="b", source="Src"),
    ]

    issue, _ = run_digest(
        feed_urls=[],
        profile=Profile(markdown="test", updated_at=datetime.now(UTC)),
        feedback=[],
        is_seen=lambda u: u == seen_url,
        rank_fn=lambda **_: _rank_response([_pick("https://a.example.com/1")]),
        summarize_fn=lambda *_a, **_k: ("s", "i"),
        discovery_fn=lambda *_a, **_k: [],
        hn_fn=lambda **_: [_article("https://hn.example.com/x")],
        websearch_fn=lambda *_a, **_k: [],
        newsletters_fn=lambda _s: [],
        serendipity_fn=lambda *_a, **_k: random_articles,
    )
    assert [str(r.url) for r in issue.random_articles] == ["https://off.example.com/fresh"]


def test_run_digest_max_web_zero_skips_websearch(env):
    from newslet.handlers.digest import run_digest

    called = []

    def spy_web(*_a, **_k):
        called.append(True)
        return []

    run_digest(
        feed_urls=[],
        profile=Profile(markdown="test", updated_at=datetime.now(UTC)),
        feedback=[],
        is_seen=lambda _: False,
        rank_fn=lambda **_: _rank_response([_pick("https://a.example.com/1")]),
        summarize_fn=lambda *_a, **_k: ("s", "i"),
        discovery_fn=lambda *_a, **_k: [],
        hn_fn=lambda **_: [_article("https://hn.example.com/x")],
        websearch_fn=spy_web,
        newsletters_fn=lambda _s: [],
        max_web=0,
    )
    assert called == []


# --- handler routing ---


def test_handler_raises_without_public_base_url(env, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "")
    settings.cache_clear()
    from newslet.handlers.digest import handler

    with pytest.raises(RuntimeError, match="PUBLIC_BASE_URL"):
        handler({}, None)


def test_handler_routes_manual(aws, monkeypatch):
    from newslet import db
    from newslet.handlers import digest

    db.add_feed("https://example.com/rss", title="F")
    db.put_profile("test profile")

    monkeypatch.setattr(
        digest, "run_digest",
        lambda **_: (
            Issue(
                date="2026-01-01",
                picks=[_pick("https://a.example.com/1")],
                created_at=datetime.now(UTC),
            ),
            [_article("https://a.example.com/1")],
        ),
    )
    sent = []
    monkeypatch.setattr(digest, "_send_email", lambda s, h: sent.append(s))
    monkeypatch.setattr(digest, "_tune_profile_after_send", lambda: None)

    result = digest.handler({"manual": True}, None)
    assert result["status"] == "sent"
    assert "manual-" in result["date"]
    assert len(sent) == 1


def test_handler_ignores_stale_home_event(aws, monkeypatch):
    # A leftover {"home": true} invoke (e.g. an in-flight async event during
    # deploy) must fall through to the idempotent daily path, not crash.
    from newslet.handlers import digest

    monkeypatch.setattr(digest.db, "issue_sent", lambda _d: True)
    result = digest.handler({"home": True}, None)
    assert result["status"] == "already_sent"


def test_handler_routes_discover(aws, monkeypatch):
    from datetime import UTC, datetime

    from newslet import db
    from newslet.contracts import DiscoverAccount, DiscoverBoard, DiscoverFeed
    from newslet.handlers import digest

    db.add_feed("https://example.com/rss", title="F")
    db.put_profile("test profile")

    board = DiscoverBoard(
        feeds=[
            DiscoverFeed(
                title="New Wire",
                site_url="https://new.example.com",
                feed_url="https://new.example.com/rss",
            )
        ],
        accounts=[
            DiscoverAccount(handle="newuser", url="https://x.com/newuser")
        ],
        generated_at=datetime.now(UTC),
    )
    monkeypatch.setattr(digest.discover, "build_discover_board", lambda *_a, **_k: board)

    result = digest.handler({"discover": True}, None)
    assert result["status"] == "discover_refreshed"

    stored = db.get_discover()
    assert stored.generated_at is not None
    assert len(stored.feeds) == 1
    assert stored.feeds[0].title == "New Wire"
    assert len(stored.accounts) == 1


def test_handler_routes_discover_keeps_previous_board_on_failed_build(aws, monkeypatch):
    from datetime import UTC, datetime

    from newslet import db
    from newslet.contracts import DiscoverBoard, DiscoverFeed
    from newslet.handlers import digest

    db.add_feed("https://example.com/rss", title="F")
    db.put_profile("test profile")

    previous = DiscoverBoard(
        feeds=[
            DiscoverFeed(
                title="Old Wire",
                site_url="https://old.example.com",
                feed_url="https://old.example.com/rss",
            )
        ],
        generated_at=datetime.now(UTC),
    )
    db.put_discover(previous)

    # A failed build yields an empty board with generated_at=None.
    monkeypatch.setattr(
        digest.discover, "build_discover_board", lambda *_a, **_k: DiscoverBoard()
    )

    result = digest.handler({"discover": True}, None)
    assert result["status"] == "discover_failed"

    stored = db.get_discover()
    assert stored.generated_at is not None
    assert len(stored.feeds) == 1
    assert stored.feeds[0].title == "Old Wire"


def test_handler_daily_already_sent(aws, monkeypatch):
    from newslet import db
    from newslet.handlers import digest

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    # Pre-store an issue and mark it sent
    issue = Issue(
        date=today,
        picks=[_pick("https://a.example.com/1")],
        created_at=datetime.now(UTC),
    )
    db.put_issue(issue)
    db.mark_issue_sent(today)

    result = digest.handler({}, None)
    assert result["status"] == "already_sent"


# --- _tune_profile_after_send exception swallowing ---


def test_tune_profile_exception_is_swallowed(aws, monkeypatch):
    from newslet import db, tune
    from newslet.handlers.digest import _tune_profile_after_send

    db.put_profile("my profile")
    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(tune, "tune_profile", _boom)

    # Must not raise
    _tune_profile_after_send()


# --- CLI fakes ---


def test_fake_rank_returns_deterministic_picks():
    from newslet.handlers.digest import _fake_rank

    candidates = [_article(f"https://example.com/{i}") for i in range(15)]
    result = _fake_rank("profile", [], candidates)
    assert len(result.picks) == 10
    assert result.picks[0].score == 1.0


def test_fake_summarize_returns_subject_intro():
    from newslet.handlers.digest import _fake_summarize

    picks = [_pick("https://a.example.com/1", "Big Story")]
    subject, intro = _fake_summarize(picks)
    assert "Big Story" in subject
    assert "1 stories" in intro


def test_fake_summarize_empty_picks():
    from newslet.handlers.digest import _fake_summarize

    subject, intro = _fake_summarize([])
    assert subject == ""
    assert intro == ""


def test_fake_discoveries_returns_list():
    from newslet.handlers.digest import _fake_discoveries

    result = _fake_discoveries("profile", ["example.com"])
    assert len(result) == 1
    assert result[0].source == "Example Wire"


def test_fake_hn_returns_articles():
    from newslet.handlers.digest import _fake_hn

    result = _fake_hn(pages=5)
    assert len(result) == 1
    assert "Hacker News" in result[0].source


def test_fake_newsletters_returns_articles():
    from newslet.handlers.digest import _fake_newsletters

    result = _fake_newsletters(datetime.now(UTC))
    assert len(result) == 1
    assert "Newsletter" in result[0].source


def test_fake_websearch_returns_web_articles():
    from newslet.handlers.digest import _fake_websearch

    result = _fake_websearch("query")
    assert len(result) == 1
    assert "web" in result[0].source.lower()


# --- CLI main ---


def test_main_dry_run(env, monkeypatch, tmp_path):
    from newslet.handlers import digest

    feeds_file = tmp_path / "feeds.txt"
    feeds_file.write_text("https://example.com/rss\n")
    profile_file = tmp_path / "profile.md"
    profile_file.write_text("I like tech\n")
    out_file = tmp_path / "out" / "email.html"

    # Stub feedparser so it returns a fixture with a fresh entry
    from newslet import feeds as feeds_mod

    now = datetime.now(UTC)
    fresh_struct = (now - timedelta(hours=2)).utctimetuple()
    monkeypatch.setattr(
        feeds_mod, "feedparser",
        SimpleNamespace(parse=lambda _u: SimpleNamespace(
            bozo=0, bozo_exception=None,
            feed={"title": "Test"},
            entries=[{
                "link": "https://example.com/fresh-1",
                "title": "Fresh One",
                "summary": "summary",
                "published_parsed": fresh_struct,
            }],
        )),
    )

    exit_code = digest.main([
        "--dry-run",
        "--feeds", str(feeds_file),
        "--profile", str(profile_file),
        "--out", str(out_file),
    ])
    assert exit_code == 0
    assert out_file.exists()
    content = out_file.read_text()
    assert "Fresh One" in content


def test_main_dry_run_no_picks(env, monkeypatch, tmp_path, capsys):
    from newslet.handlers import digest

    feeds_file = tmp_path / "feeds.txt"
    feeds_file.write_text("https://example.com/rss\n")
    out_file = tmp_path / "out" / "email.html"

    # Stub feedparser to return no entries
    from newslet import feeds as feeds_mod

    monkeypatch.setattr(
        feeds_mod, "feedparser",
        SimpleNamespace(parse=lambda _u: SimpleNamespace(
            bozo=0, bozo_exception=None,
            feed={"title": "Test"},
            entries=[],
        )),
    )
    # Override the fakes to also return nothing so there are truly no candidates
    monkeypatch.setattr(digest, "_fake_hn", lambda **_: [])
    monkeypatch.setattr(digest, "_fake_newsletters", lambda _s, **_: [])
    monkeypatch.setattr(digest, "_fake_websearch", lambda *_a, **_k: [])
    monkeypatch.setattr(digest, "_fake_x", lambda *_a, **_k: [])

    exit_code = digest.main([
        "--dry-run",
        "--feeds", str(feeds_file),
        "--profile", str(tmp_path / "nonexistent.md"),
        "--out", str(out_file),
    ])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "no picks today" in captured.out


# --- Tech facts: wiring, feedback separation, tune ---


def _fb(url: str, rating: str = "up", title: str = "T"):
    from newslet.contracts import FeedbackRow

    return FeedbackRow(
        article_url=url, title=title, rating=rating,
        ts=datetime.now(UTC), issue_date="2026-08-06",
    )


def test_split_feedback_routes_by_path():
    from newslet.handlers.digest import _split_feedback

    general = _fb("https://ex.com/article")
    fact = _fb("https://api.example.com/facts/2026-08-06/mid")
    quote = _fb("https://api.example.com/quote/2026-08-06")
    got_general, got_facts, got_quotes = _split_feedback([general, fact, quote])
    assert [str(r.article_url) for r in got_general] == ["https://ex.com/article"]
    assert [str(r.article_url) for r in got_facts] == [
        "https://api.example.com/facts/2026-08-06/mid"
    ]
    assert [str(r.article_url) for r in got_quotes] == [
        "https://api.example.com/quote/2026-08-06"
    ]


def _facts_pair():
    from newslet.contracts import Fact

    return [
        Fact(title="Mid fact", body_md="B", genre="algorithms & math", slot="mid"),
        Fact(title="End fact", body_md="B", genre="computing history & lore", slot="end"),
    ]


def test_run_digest_attaches_facts(env):
    from newslet.handlers.digest import run_digest

    captured = {}

    def fake_facts(profile_md, topics, **_):
        captured["args"] = (profile_md, topics)
        return _facts_pair()

    issue, _ = run_digest(
        feed_urls=[],
        profile=Profile(markdown="test", updated_at=datetime.now(UTC)),
        feedback=[],
        is_seen=lambda _: False,
        rank_fn=lambda **_: _rank_response([_pick("https://a.example.com/1")]),
        summarize_fn=lambda *_a, **_k: ("s", "i"),
        discovery_fn=lambda *_a, **_k: [],
        hn_fn=lambda **_: [_article("https://hn.example.com/x")],
        websearch_fn=lambda *_a, **_k: [],
        serendipity_fn=lambda *_a, **_k: [],
        newsletters_fn=lambda _s: [],
        facts_fn=fake_facts,
        facts_profile_md="- loves lore",
        facts_recent_topics=["Old"],
    )
    assert [f.slot for f in issue.facts] == ["mid", "end"]
    assert captured["args"] == ("- loves lore", ["Old"])


def test_run_digest_facts_exception_is_swallowed(env):
    from newslet.handlers.digest import run_digest

    def boom_facts(*_a, **_k):
        raise RuntimeError("facts down")

    issue, _ = run_digest(
        feed_urls=[],
        profile=Profile(markdown="test", updated_at=datetime.now(UTC)),
        feedback=[],
        is_seen=lambda _: False,
        rank_fn=lambda **_: _rank_response([_pick("https://a.example.com/1")]),
        summarize_fn=lambda *_a, **_k: ("s", "i"),
        discovery_fn=lambda *_a, **_k: [],
        hn_fn=lambda **_: [_article("https://hn.example.com/x")],
        websearch_fn=lambda *_a, **_k: [],
        serendipity_fn=lambda *_a, **_k: [],
        newsletters_fn=lambda _s: [],
        facts_fn=boom_facts,
    )
    assert issue.facts == []


def test_run_digest_facts_disabled_skips_call(env):
    from newslet.handlers.digest import run_digest

    def must_not_run(*_a, **_k):
        raise AssertionError("facts_fn must not be called when disabled")

    issue, _ = run_digest(
        feed_urls=[],
        profile=Profile(markdown="test", updated_at=datetime.now(UTC)),
        feedback=[],
        is_seen=lambda _: False,
        rank_fn=lambda **_: _rank_response([_pick("https://a.example.com/1")]),
        summarize_fn=lambda *_a, **_k: ("s", "i"),
        discovery_fn=lambda *_a, **_k: [],
        hn_fn=lambda **_: [_article("https://hn.example.com/x")],
        websearch_fn=lambda *_a, **_k: [],
        serendipity_fn=lambda *_a, **_k: [],
        newsletters_fn=lambda _s: [],
        facts_fn=must_not_run,
        facts_enabled=False,
    )
    assert issue.facts == []


def test_fresh_issue_does_not_burn_topics_before_send(aws, monkeypatch):
    """The no-repeat log advances only after a confirmed send — a build whose
    send later fails must not burn topics no reader saw."""
    from newslet import db
    from newslet.contracts import FactsState
    from newslet.handlers import digest

    db.put_facts_state(FactsState(markdown="- m", recent_topics=["t0"]))
    fake_issue = Issue(
        date="2026-08-06",
        picks=[_pick("https://a.example.com/1")],
        created_at=datetime.now(UTC),
        facts=_facts_pair(),
    )
    monkeypatch.setattr(digest, "run_digest", lambda **_: (fake_issue, []))

    issue, _ = digest._fresh_issue()
    assert [f.slot for f in issue.facts] == ["mid", "end"]
    assert db.get_facts_state().recent_topics == ["t0"]


def test_advance_facts_topic_log_dedupes_and_caps(aws):
    from newslet import db
    from newslet.contracts import FactsState
    from newslet.handlers import digest

    db.put_facts_state(
        FactsState(markdown="- m", recent_topics=[f"t{i}" for i in range(59)])
    )
    issue = Issue(
        date="2026-08-06",
        picks=[],
        created_at=datetime.now(UTC),
        facts=_facts_pair(),
    )
    digest._advance_facts_topic_log(issue)
    state = db.get_facts_state()
    assert len(state.recent_topics) == 60  # capped
    assert state.recent_topics[-2:] == ["Mid fact", "End fact"]
    assert state.markdown == "- m"  # never touches the taste profile

    # A duplicate-send retry appends nothing new.
    digest._advance_facts_topic_log(issue)
    assert db.get_facts_state().recent_topics == state.recent_topics


def test_fresh_issue_passes_general_feedback_only(aws, monkeypatch):
    from newslet import db
    from newslet.handlers import digest

    db.put_feedback(_fb("https://ex.com/article", title="General"))
    db.put_feedback(_fb("https://api.example.com/facts/2026-08-06/mid", title="Fact"))

    captured = {}

    def fake_run_digest(**kwargs):
        captured["feedback"] = kwargs["feedback"]
        return (
            Issue(date="2026-08-06", picks=[], created_at=datetime.now(UTC)),
            [],
        )

    monkeypatch.setattr(digest, "run_digest", fake_run_digest)
    digest._fresh_issue()
    assert [r.title for r in captured["feedback"]] == ["General"]


def test_tune_facts_after_send_updates_only_facts_row(aws, monkeypatch):
    from newslet import db, facts
    from newslet.contracts import FactsState
    from newslet.handlers import digest

    db.put_profile("# me")
    db.put_facts_state(FactsState(markdown="- old", recent_topics=["a"]))
    db.put_feedback(_fb("https://api.example.com/facts/2026-08-06/mid", title="Fact"))
    db.put_feedback(_fb("https://ex.com/article", title="General"))

    captured = {}

    def fake_tune(current_md, feedback, **_):
        captured["md"] = current_md
        captured["titles"] = [r.title for r in feedback]
        return "- new understanding"

    monkeypatch.setattr(facts, "tune_facts_profile", fake_tune)
    digest._tune_facts_after_send()

    assert captured["md"] == "- old"
    assert captured["titles"] == ["Fact"]  # general votes never reach the facts tuner
    state = db.get_facts_state()
    assert state.markdown == "- new understanding"
    assert state.recent_topics == ["a"]  # log survives the tune
    assert db.get_profile().markdown == "# me"  # general profile untouched


def test_tune_profile_after_send_excludes_fact_votes(aws, monkeypatch):
    from newslet import db, tune
    from newslet.handlers import digest

    db.put_profile("# me")
    db.put_feedback(_fb("https://api.example.com/facts/2026-08-06/mid", title="Fact"))
    db.put_feedback(_fb("https://ex.com/article", title="General"))

    captured = {}

    def fake_tune(profile_md, feedback, **_):
        captured["titles"] = [r.title for r in feedback]
        return profile_md

    monkeypatch.setattr(tune, "tune_profile", fake_tune)
    digest._tune_profile_after_send()
    assert captured["titles"] == ["General"]


def test_split_feedback_keeps_external_facts_paths_general():
    """A real article that happens to live under /facts/ or /quote/ on some
    site must stay in the general bucket — only the app's exact synthetic
    shapes (/facts/{issue-key}/{slot}, /quote/{issue-key}) are rerouted."""
    from newslet.handlers.digest import _split_feedback

    rows = [
        _fb("https://example.com/facts/tcp"),
        _fb("https://example.com/quote/of-the-day-history"),
        _fb("https://example.com/facts/2026/BAD/extra/mid"),
        _fb("https://api.example.com/facts/manual-20260806-042944-7c43c81f/end"),
    ]
    general, fact_rows, quote_rows = _split_feedback(rows)
    assert [str(r.article_url) for r in fact_rows] == [
        "https://api.example.com/facts/manual-20260806-042944-7c43c81f/end"
    ]
    assert quote_rows == []
    assert len(general) == 3


def test_recent_feedback_split_overfetches_so_facts_cannot_starve_ranking(
    aws, monkeypatch
):
    from newslet import db
    from newslet.handlers import digest

    # Newest-first stream: 6 fact votes ahead of 3 article votes.
    rows = [
        _fb(f"https://api.example.com/facts/2026-08-0{i % 9 + 1}/mid", title=f"F{i}")
        for i in range(6)
    ] + [_fb(f"https://ex.com/a{i}", title=f"G{i}") for i in range(3)]

    captured = {}

    def fake_recent_feedback(limit):
        captured["limit"] = limit
        return rows[:limit]

    monkeypatch.setattr(digest.db, "recent_feedback", fake_recent_feedback)
    general, fact_rows, _quote_rows = digest._recent_feedback_split(2)
    # Over-fetched past the 6-fact streak…
    assert captured["limit"] == 2 * digest._SPLIT_FETCH_MULTIPLIER
    # …so the general bucket still fills, trimmed newest-first per bucket.
    assert [r.title for r in general] == ["G0", "G1"]
    assert [r.title for r in fact_rows] == ["F0", "F1"]
    assert db is not None  # keep the aws fixture meaningfully used


# --- Quote of the day: wiring, log, tune ---


def _quote():
    from newslet.contracts import Quote

    return Quote(text="Be here now, fully.", author="Ram Dass",
                 source="Be Here Now", tradition="kin")


def test_run_digest_attaches_quote(env):
    from newslet.handlers.digest import run_digest

    captured = {}

    def fake_quote(profile_md, recent, **_):
        captured["args"] = (profile_md, recent)
        return _quote()

    issue, _ = run_digest(
        feed_urls=[],
        profile=Profile(markdown="test", updated_at=datetime.now(UTC)),
        feedback=[],
        is_seen=lambda _: False,
        rank_fn=lambda **_: _rank_response([_pick("https://a.example.com/1")]),
        summarize_fn=lambda *_a, **_k: ("s", "i"),
        discovery_fn=lambda *_a, **_k: [],
        hn_fn=lambda **_: [_article("https://hn.example.com/x")],
        websearch_fn=lambda *_a, **_k: [],
        serendipity_fn=lambda *_a, **_k: [],
        newsletters_fn=lambda _s: [],
        facts_fn=lambda *_a, **_k: [],
        quote_fn=fake_quote,
        quotes_profile_md="- likes Stoics",
        recent_quotes=["Old — entry"],
    )
    assert issue.quote is not None and issue.quote.author == "Ram Dass"
    assert captured["args"] == ("- likes Stoics", ["Old — entry"])


def test_run_digest_quote_exception_and_disable(env):
    from newslet.handlers.digest import run_digest

    common = dict(
        feed_urls=[],
        profile=Profile(markdown="test", updated_at=datetime.now(UTC)),
        feedback=[],
        is_seen=lambda _: False,
        rank_fn=lambda **_: _rank_response([_pick("https://a.example.com/1")]),
        summarize_fn=lambda *_a, **_k: ("s", "i"),
        discovery_fn=lambda *_a, **_k: [],
        hn_fn=lambda **_: [_article("https://hn.example.com/x")],
        websearch_fn=lambda *_a, **_k: [],
        serendipity_fn=lambda *_a, **_k: [],
        newsletters_fn=lambda _s: [],
        facts_fn=lambda *_a, **_k: [],
    )

    def boom(*_a, **_k):
        raise RuntimeError("down")

    issue, _ = run_digest(**common, quote_fn=boom)
    assert issue.quote is None

    def must_not_run(*_a, **_k):
        raise AssertionError("quote_fn must not run when disabled")

    issue, _ = run_digest(**common, quote_fn=must_not_run, quote_enabled=False)
    assert issue.quote is None


def test_advance_quotes_log_dedupes_and_caps(aws):
    from newslet import db
    from newslet.contracts import QuotesState
    from newslet.handlers import digest

    db.put_quotes_state(
        QuotesState(markdown="- m", recent_quotes=[f"q{i}" for i in range(119)])
    )
    issue = Issue(
        date="2026-08-08", picks=[], created_at=datetime.now(UTC), quote=_quote()
    )
    digest._advance_quotes_log(issue)
    state = db.get_quotes_state()
    assert len(state.recent_quotes) == 120
    assert state.recent_quotes[-1].startswith("Ram Dass — ")
    assert state.markdown == "- m"

    digest._advance_quotes_log(issue)
    assert db.get_quotes_state().recent_quotes == state.recent_quotes


def test_tune_quotes_after_send_updates_only_quotes_row(aws, monkeypatch):
    from newslet import db, quotes
    from newslet.contracts import QuotesState
    from newslet.handlers import digest

    db.put_profile("# me")
    db.put_quotes_state(QuotesState(markdown="- old", recent_quotes=["a"]))
    db.put_feedback(_fb("https://api.example.com/quote/2026-08-08", title="Quote: X"))
    db.put_feedback(_fb("https://ex.com/article", title="General"))

    captured = {}

    def fake_tune(current_md, feedback, **_):
        captured["titles"] = [r.title for r in feedback]
        return "- new"

    monkeypatch.setattr(quotes, "tune_quotes_profile", fake_tune)
    digest._tune_quotes_after_send()

    assert captured["titles"] == ["Quote: X"]
    state = db.get_quotes_state()
    assert state.markdown == "- new"
    assert state.recent_quotes == ["a"]
    assert db.get_profile().markdown == "# me"


# --- Weather line ---


def test_run_digest_attaches_weather_line(env):
    from newslet.handlers.digest import run_digest

    common = dict(
        feed_urls=[],
        profile=Profile(markdown="test", updated_at=datetime.now(UTC)),
        feedback=[],
        is_seen=lambda _: False,
        rank_fn=lambda **_: _rank_response([_pick("https://a.example.com/1")]),
        summarize_fn=lambda *_a, **_k: ("s", "i"),
        discovery_fn=lambda *_a, **_k: [],
        hn_fn=lambda **_: [_article("https://hn.example.com/x")],
        websearch_fn=lambda *_a, **_k: [],
        serendipity_fn=lambda *_a, **_k: [],
        newsletters_fn=lambda _s: [],
        facts_fn=lambda *_a, **_k: [],
        quote_fn=lambda *_a, **_k: None,
    )
    issue, _ = run_digest(**common, weather_fn=lambda **_: "78° sunny, tonight 64° clear")
    assert issue.weather_line == "78° sunny, tonight 64° clear"

    def boom(**_):
        raise RuntimeError("nws down")

    issue, _ = run_digest(**common, weather_fn=boom)
    assert issue.weather_line == ""

    def must_not_run(**_):
        raise AssertionError("weather_fn must not run when disabled")

    issue, _ = run_digest(**common, weather_fn=must_not_run, weather_enabled=False)
    assert issue.weather_line == ""

    # None from the fetcher stores as the empty string, not "None".
    issue, _ = run_digest(**common, weather_fn=lambda **_: None)
    assert issue.weather_line == ""
