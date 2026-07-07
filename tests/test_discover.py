"""Tests for :mod:`newslet.discover`.

The fake client mimics the subset of :class:`anthropic.Anthropic` that
:func:`newslet.discover.build_discover_board` uses, so no env/network/API
key is needed. Mirrors the fake-client style of ``tests/test_discovery.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from newslet import discover
from newslet.discover import build_discover_board


class FakeClient:
    """Stand-in for :class:`anthropic.Anthropic` recording every call."""

    def __init__(self, content: list):
        self._content = content
        self.calls: list[dict] = []

    @property
    def messages(self):
        return self

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=self._content)


def _text_only(json_str: str) -> list:
    return [SimpleNamespace(type="text", text=json_str)]


def _live_validator(_url: str) -> bool:
    return True


# ---------- fixtures ----------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_settings(monkeypatch):
    """Make settings() succeed even though we always pass client=fake.

    build_discover_board() reads ``settings().claude_model``, so the env
    must be populated even when an explicit client is provided.
    """
    env = {
        "ANTHROPIC_API_KEY": "test-key",
        "CLAUDE_MODEL": "claude-test",
        "RESEND_API_KEY": "x",
        "FROM_EMAIL": "a@b.c",
        "TO_EMAIL": "a@b.c",
        "ADMIN_TOKEN": "x",
        "SIGNING_KEY": "x",
        "PUBLIC_BASE_URL": "https://example.com",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from newslet.config import settings

    settings.cache_clear()
    yield
    settings.cache_clear()


_NOW = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)


def _payload(feeds=None, accounts=None) -> dict:
    return {"feeds": feeds or [], "accounts": accounts or []}


def _feed(i: int, site: str | None = None) -> dict:
    site = site or f"site{i}.com"
    return {
        "title": f"Feed {i}",
        "site_url": f"https://{site}/",
        "feed_url": f"https://{site}/feed.xml",
        "reason": "Matches the profile.",
    }


def _account(handle: str, name: str = "Name", reason: str = "Fits.") -> dict:
    return {"handle": handle, "name": name, "reason": reason}


# ---------- happy path ----------------------------------------------------


def test_happy_path_returns_feeds_and_accounts():
    payload = _payload(
        feeds=[_feed(1), _feed(2)],
        accounts=[_account("alice"), _account("bob")],
    )
    fake = FakeClient(_text_only(json.dumps(payload)))

    board = build_discover_board(
        "my profile", [], client=fake, feed_validator=_live_validator, now=_NOW
    )

    assert len(board.feeds) == 2
    assert len(board.accounts) == 2
    assert board.generated_at == _NOW
    assert str(board.feeds[0].site_url) == "https://site1.com/"
    assert str(board.feeds[0].feed_url) == "https://site1.com/feed.xml"
    assert board.feeds[0].reason == "Matches the profile."
    assert board.accounts[0].handle == "alice"
    assert str(board.accounts[0].url) == "https://x.com/alice"
    # The web search tool must be enabled on the request.
    tools = fake.calls[0]["tools"]
    assert tools[0]["name"] == "web_search"
    assert fake.calls[0]["model"] == "claude-test"
    assert fake.calls[0]["max_tokens"] == 4096


# ---------- feed exclusion / validation -----------------------------------


def test_excludes_feed_on_already_followed_domain():
    payload = _payload(
        feeds=[
            _feed(1, site="known.com"),
            _feed(2, site="fresh.io"),
        ]
    )
    fake = FakeClient(_text_only(json.dumps(payload)))

    board = build_discover_board(
        "p", ["known.com"], client=fake, feed_validator=_live_validator, now=_NOW
    )

    assert len(board.feeds) == 1
    assert str(board.feeds[0].site_url) == "https://fresh.io/"


def test_drops_feed_whose_validator_returns_false():
    payload = _payload(
        feeds=[_feed(1, site="dead.com"), _feed(2, site="live.com")]
    )
    fake = FakeClient(_text_only(json.dumps(payload)))

    def validator(url: str) -> bool:
        return "live.com" in url

    board = build_discover_board(
        "p", [], client=fake, feed_validator=validator, max_feeds=5, now=_NOW
    )

    assert len(board.feeds) == 1
    assert str(board.feeds[0].site_url) == "https://live.com/"


def test_feed_validator_called_lazily_stops_at_max_feeds():
    payload = _payload(feeds=[_feed(i) for i in range(5)])
    fake = FakeClient(_text_only(json.dumps(payload)))

    checked: list[str] = []

    def validator(url: str) -> bool:
        checked.append(url)
        return True

    board = build_discover_board(
        "p", [], client=fake, feed_validator=validator, max_feeds=2, now=_NOW
    )

    assert len(board.feeds) == 2
    assert checked == ["https://site0.com/feed.xml", "https://site1.com/feed.xml"]


def test_drops_feed_missing_feed_url():
    payload = _payload(
        feeds=[
            {"title": "NoFeed", "site_url": "https://nofeed.com/", "reason": "r"},
            _feed(1, site="hasfeed.com"),
        ]
    )
    fake = FakeClient(_text_only(json.dumps(payload)))

    board = build_discover_board(
        "p", [], client=fake, feed_validator=_live_validator, max_feeds=5, now=_NOW
    )

    assert len(board.feeds) == 1
    assert str(board.feeds[0].site_url) == "https://hasfeed.com/"


# ---------- account normalization / dedup ---------------------------------


def test_account_handle_normalized_strips_at_and_whitespace():
    payload = _payload(accounts=[_account("  @some_user  ")])
    fake = FakeClient(_text_only(json.dumps(payload)))

    board = build_discover_board("p", [], client=fake, now=_NOW)

    assert len(board.accounts) == 1
    assert board.accounts[0].handle == "some_user"
    assert str(board.accounts[0].url) == "https://x.com/some_user"


def test_invalid_handle_too_long_or_with_spaces_dropped():
    payload = _payload(
        accounts=[
            _account("way too long handle!!!"),
            _account("this_handle_is_too_long_for_x"),
            _account("valid_one"),
        ]
    )
    fake = FakeClient(_text_only(json.dumps(payload)))

    board = build_discover_board("p", [], client=fake, now=_NOW)

    assert [a.handle for a in board.accounts] == ["valid_one"]


def test_duplicate_handles_deduped_case_insensitive():
    payload = _payload(
        accounts=[_account("Alice"), _account("alice"), _account("ALICE")]
    )
    fake = FakeClient(_text_only(json.dumps(payload)))

    board = build_discover_board("p", [], client=fake, now=_NOW)

    assert len(board.accounts) == 1
    assert board.accounts[0].handle == "Alice"


# ---------- caps -----------------------------------------------------------


def test_max_feeds_and_max_accounts_cap_results():
    payload = _payload(
        feeds=[_feed(i) for i in range(5)],
        accounts=[_account(f"user{i}") for i in range(5)],
    )
    fake = FakeClient(_text_only(json.dumps(payload)))

    board = build_discover_board(
        "p",
        [],
        client=fake,
        feed_validator=_live_validator,
        max_feeds=2,
        max_accounts=3,
        now=_NOW,
    )

    assert len(board.feeds) == 2
    assert len(board.accounts) == 3


# ---------- failure modes ---------------------------------------------------


def test_no_json_returns_empty_board_with_no_generated_at():
    fake = FakeClient(_text_only("not json at all"))

    board = build_discover_board("p", [], client=fake, now=_NOW)

    assert board == discover.DiscoverBoard()
    assert board.feeds == []
    assert board.accounts == []
    assert board.generated_at is None


def test_api_exception_returns_empty_board():
    class _Boom:
        @property
        def messages(self):
            return self

        def create(self, **_):
            raise RuntimeError("rate limited")

    board = build_discover_board("p", [], client=_Boom(), now=_NOW)

    assert board.feeds == []
    assert board.accounts == []
    assert board.generated_at is None


def test_no_text_block_returns_empty_board():
    fake = FakeClient([SimpleNamespace(type="server_tool_use", name="web_search")])

    board = build_discover_board("p", [], client=fake, now=_NOW)

    assert board.generated_at is None


def test_malformed_items_partial_keeps_good_items():
    payload = {
        "feeds": [
            {"title": "Bad", "site_url": "not-a-url", "feed_url": "also-bad"},
            _feed(1, site="good.com"),
        ],
        "accounts": [
            {"handle": ""},
            _account("gooduser"),
        ],
    }
    fake = FakeClient(_text_only(json.dumps(payload)))

    board = build_discover_board(
        "p", [], client=fake, feed_validator=_live_validator, now=_NOW
    )

    assert len(board.feeds) == 1
    assert str(board.feeds[0].site_url) == "https://good.com/"
    assert len(board.accounts) == 1
    assert board.accounts[0].handle == "gooduser"
    assert board.generated_at == _NOW


def test_reads_last_text_block_amid_tool_blocks():
    payload = _payload(feeds=[_feed(1)], accounts=[_account("someone")])
    content = [
        SimpleNamespace(type="text", text="Let me search for that."),
        SimpleNamespace(type="server_tool_use", name="web_search"),
        SimpleNamespace(type="web_search_tool_result"),
        SimpleNamespace(type="text", text=json.dumps(payload)),
    ]
    fake = FakeClient(content)

    board = build_discover_board(
        "p", [], client=fake, feed_validator=_live_validator, now=_NOW
    )

    assert len(board.feeds) == 1
    assert len(board.accounts) == 1


def test_parses_fenced_json():
    payload = _payload(feeds=[_feed(1)], accounts=[_account("someone")])
    fenced = "```json\n" + json.dumps(payload) + "\n```"
    fake = FakeClient(_text_only(fenced))

    board = build_discover_board(
        "p", [], client=fake, feed_validator=_live_validator, now=_NOW
    )

    assert len(board.feeds) == 1
    assert len(board.accounts) == 1


# ---------- the moved helper ------------------------------------------------


def test_feed_is_live_lives_in_search_common():
    """search_common.feed_is_live exists and is what discover/discovery use
    by default (the liveness check itself is exercised in
    test_search_helpers.py; this just pins the new home)."""
    from newslet.search_common import feed_is_live

    assert discover.feed_is_live is feed_is_live
