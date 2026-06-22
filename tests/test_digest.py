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


def test_handler_routes_home(aws, monkeypatch):
    from newslet import db
    from newslet.handlers import digest

    db.add_feed("https://example.com/rss", title="F")
    db.put_profile("test profile")

    monkeypatch.setattr(
        digest, "run_digest",
        lambda **_: (
            Issue(
                date="home",
                picks=[_pick("https://a.example.com/1")],
                created_at=datetime.now(UTC),
            ),
            [_article("https://a.example.com/1")],
        ),
    )

    result = digest.handler({"home": True}, None)
    assert result["status"] == "home_refreshed"


def test_handler_routes_x_discover(aws, monkeypatch):
    """The x_discover mode finds accounts, excludes follows + the shown page,
    and stores the result as the prefetched next page."""
    from newslet import db, x_discover
    from newslet.contracts import XAccount, XPost
    from newslet.handlers import digest

    db.put_profile("AI safety, distributed systems")
    db.add_x_follow("alreadyfollowed")
    db.set_x_discover_next([
        XAccount(handle="onpage", url="https://x.com/onpage",
                 posts=[XPost(url="https://x.com/onpage/status/1", text="hi")])
    ])
    db.promote_x_discover()  # "onpage" is now the current (shown) page

    captured: dict = {}

    def fake_find(query, *, exclude_handles, max_results, **_):
        captured["query"] = query
        captured["exclude"] = set(exclude_handles)
        return [
            XAccount(handle="fresh", url="https://x.com/fresh",
                     posts=[XPost(url="https://x.com/fresh/status/2", text="new")])
        ]

    monkeypatch.setattr(x_discover, "find_x_accounts", fake_find)

    result = digest.handler({"x_discover": True}, None)
    assert result == {"status": "x_discover_refreshed", "accounts": 1}
    # Followed + already-shown handles are excluded from the search.
    assert {"alreadyfollowed", "onpage"} <= captured["exclude"]
    assert "AI safety" in captured["query"]
    # The new page is buffered as "next", leaving the shown page intact.
    state = db.get_x_discover()
    assert [a.handle for a in state["current"]] == ["onpage"]
    assert [a.handle for a in state["next"]] == ["fresh"]


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
