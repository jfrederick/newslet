"""Tests for X (Twitter) account discovery (:mod:`newslet.x_discover`).

The xAI endpoint is the only network edge; it is injected as a ``complete``
callable so these stay offline — mirroring :mod:`tests.test_x_grok`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from newslet import x_discover
from newslet.config import settings


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("RESEND_API_KEY", "x")
    monkeypatch.setenv("FROM_EMAIL", "f@example.com")
    monkeypatch.setenv("TO_EMAIL", "t@example.com")
    monkeypatch.setenv("ADMIN_TOKEN", "x")
    monkeypatch.setenv("SIGNING_KEY", "k")
    monkeypatch.setenv("XAI_API_KEY", "xai-test-key")
    settings.cache_clear()
    yield
    settings.cache_clear()


_NOW = datetime(2026, 6, 22, tzinfo=UTC)


def _post(url: str, *, text: str = "A sharp take", posted_at: str | None = "2026-06-20",
          likes: int = 100, reposts: int = 5) -> dict:
    p = {"url": url, "text": text, "likes": likes, "reposts": reposts}
    if posted_at is not None:
        p["posted_at"] = posted_at
    return p


def _account(handle: str, *, name: str = "Expert", bio: str = "Bio", reason: str = "Relevant",
             posts: list[dict] | None = None) -> dict:
    return {
        "handle": handle,
        "name": name,
        "bio": bio,
        "reason": reason,
        "posts": posts if posts is not None else [_post(f"https://x.com/{handle.lstrip('@')}/status/1")],
    }


def _reply(*accounts: dict) -> str:
    return json.dumps({"accounts": list(accounts)})


def _fake_complete(reply: str):
    calls: list[dict] = []

    def complete(payload: dict, api_key: str) -> dict:
        calls.append({"payload": payload, "api_key": api_key})
        return {
            "output": [
                {"type": "reasoning", "content": []},
                {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": reply}]},
            ]
        }

    complete.calls = calls  # type: ignore[attr-defined]
    return complete


def test_parses_accounts(env):
    complete = _fake_complete(_reply(
        _account("@alice", name="Alice"),
        _account("bob"),
    ))
    out = x_discover.find_x_accounts("AI policy", complete=complete, now=_NOW)
    assert [a.handle for a in out] == ["alice", "bob"]
    assert str(out[0].url) == "https://x.com/alice"
    assert out[0].name == "Alice"
    assert out[0].posts[0].likes == 100
    assert str(out[0].posts[0].url) == "https://x.com/alice/status/1"


def test_normalizes_handle_from_url_and_at(env):
    complete = _fake_complete(_reply(
        _account("https://x.com/CarolDev/"),
        _account("@DanResearch"),
    ))
    out = x_discover.find_x_accounts("topic", complete=complete, now=_NOW)
    assert [a.handle for a in out] == ["caroldev", "danresearch"]


def test_drops_account_without_recent_posts(env):
    """An account whose only post predates the window is dropped entirely."""
    stale = (_NOW - timedelta(days=30)).strftime("%Y-%m-%d")
    complete = _fake_complete(_reply(
        _account("dormant", posts=[_post("https://x.com/dormant/status/9", posted_at=stale)]),
        _account("active"),
    ))
    out = x_discover.find_x_accounts("topic", complete=complete, now=_NOW)
    assert [a.handle for a in out] == ["active"]


def test_keeps_posts_without_a_timestamp(env):
    """A missing posted_at is kept — from_date already bounds recency."""
    complete = _fake_complete(_reply(
        _account("nora", posts=[_post("https://x.com/nora/status/3", posted_at=None)]),
    ))
    out = x_discover.find_x_accounts("topic", complete=complete, now=_NOW)
    assert [a.handle for a in out] == ["nora"]
    assert out[0].posts[0].posted_at is None


def test_caps_posts_per_account(env):
    posts = [_post(f"https://x.com/many/status/{i}") for i in range(6)]
    complete = _fake_complete(_reply(_account("many", posts=posts)))
    out = x_discover.find_x_accounts("topic", complete=complete, now=_NOW)
    assert len(out[0].posts) == x_discover._MAX_POSTS_PER_ACCOUNT


def test_excludes_followed_handles(env):
    complete = _fake_complete(_reply(_account("@Alice"), _account("bob")))
    out = x_discover.find_x_accounts(
        "topic", complete=complete, exclude_handles={"alice"}, now=_NOW
    )
    assert [a.handle for a in out] == ["bob"]
    # The excluded handle is also surfaced to the model in the prompt.
    prompt = complete.calls[0]["payload"]["input"][0]["content"]
    assert "@alice" in prompt


def test_dedupes_by_handle_and_respects_max(env):
    complete = _fake_complete(_reply(
        _account("a"), _account("@A"), _account("b"), _account("c"),
    ))
    out = x_discover.find_x_accounts("topic", complete=complete, max_results=2, now=_NOW)
    assert [a.handle for a in out] == ["a", "b"]


def test_request_uses_x_search_tool_with_recency(env):
    complete = _fake_complete(_reply(_account("a")))
    x_discover.find_x_accounts("topic", complete=complete, model="grok-test", now=_NOW)
    call = complete.calls[0]
    assert call["api_key"] == "xai-test-key"
    payload = call["payload"]
    assert payload["model"] == "grok-test"
    assert "input" in payload and "search_parameters" not in payload
    tool = payload["tools"][0]
    assert tool["type"] == "x_search"
    # 14 days before _NOW.
    assert tool["from_date"] == (_NOW - timedelta(days=14)).strftime("%Y-%m-%d")


def test_no_api_key_short_circuits(env, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    settings.cache_clear()
    complete = _fake_complete(_reply(_account("a")))
    assert x_discover.find_x_accounts("topic", complete=complete) == []
    assert complete.calls == []


def test_empty_query_short_circuits(env):
    complete = _fake_complete("unused")
    assert x_discover.find_x_accounts("   ", complete=complete) == []
    assert complete.calls == []


def test_bad_json_returns_empty(env):
    assert x_discover.find_x_accounts("topic", complete=_fake_complete("not json")) == []


def test_api_exception_returns_empty(env):
    def boom(_payload, _key):
        raise RuntimeError("rate limited")

    assert x_discover.find_x_accounts("topic", complete=boom) == []


@pytest.mark.parametrize("output", [5, "boom", {"error": "nope"}, None])
def test_malformed_output_returns_empty(env, output):
    out = x_discover.find_x_accounts("topic", complete=lambda _p, _k: {"output": output})
    assert out == []


def test_handles_fenced_json(env):
    fenced = "Here you go:\n```json\n" + _reply(_account("a")) + "\n```\nDone."
    out = x_discover.find_x_accounts("topic", complete=_fake_complete(fenced), now=_NOW)
    assert [a.handle for a in out] == ["a"]
