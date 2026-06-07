"""Tests for the X (Twitter) source (:mod:`newslet.x_grok`).

The xAI endpoint is the only network edge; it is injected as a ``complete``
callable so these stay offline.
"""

from __future__ import annotations

import json

import pytest

from newslet import x_grok
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


def _post(url: str, *, author: str = "expert", text: str = "A useful take",
          likes: int = 100, reposts: int = 10) -> dict:
    return {"url": url, "author": author, "text": text, "likes": likes,
            "reposts": reposts}


def _reply(*posts: dict) -> str:
    return json.dumps({"posts": list(posts)})


def _fake_complete(reply: str):
    """Build a complete(payload, api_key) that returns an xAI-shaped reply."""
    calls: list[dict] = []

    def complete(payload: dict, api_key: str) -> dict:
        calls.append({"payload": payload, "api_key": api_key})
        return {"choices": [{"message": {"content": reply}}]}

    complete.calls = calls  # type: ignore[attr-defined]
    return complete


def test_parses_posts_into_articles(env):
    complete = _fake_complete(
        _reply(
            _post("https://x.com/a/status/1", author="@alice", text="Big news"),
            _post("https://x.com/b/status/2", author="bob"),
        )
    )
    out = x_grok.fetch_x_articles("AI policy", complete=complete)
    assert [str(a.url) for a in out] == [
        "https://x.com/a/status/1",
        "https://x.com/b/status/2",
    ]
    assert all(a.source == "X" for a in out)
    # Title derives from the post text; summary carries engagement signal.
    assert out[0].title == "Big news"
    assert "likes" in out[0].summary and "@alice" in out[0].summary


def test_handles_fenced_json_and_prose(env):
    fenced = "Here are the posts:\n```json\n" + _reply(
        _post("https://x.com/a/status/1")
    ) + "\n```\nHope it helps."
    out = x_grok.fetch_x_articles("topic", complete=_fake_complete(fenced))
    assert [str(a.url) for a in out] == ["https://x.com/a/status/1"]


def test_dedupes_and_respects_max_results(env):
    complete = _fake_complete(
        _reply(
            _post("https://x.com/a/status/1"),
            _post("https://x.com/a/status/1"),  # dup
            _post("https://x.com/b/status/2"),
            _post("https://x.com/c/status/3"),
        )
    )
    out = x_grok.fetch_x_articles("topic", complete=complete, max_results=2)
    assert [str(a.url) for a in out] == [
        "https://x.com/a/status/1",
        "https://x.com/b/status/2",
    ]


def test_drops_post_without_url(env):
    complete = _fake_complete(
        _reply({"author": "@nobody", "text": "no link"}, _post("https://x.com/a/status/1"))
    )
    out = x_grok.fetch_x_articles("topic", complete=complete)
    assert [str(a.url) for a in out] == ["https://x.com/a/status/1"]


def test_no_api_key_short_circuits_without_calling(env, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    settings.cache_clear()
    complete = _fake_complete(_reply(_post("https://x.com/a/status/1")))
    out = x_grok.fetch_x_articles("topic", complete=complete)
    assert out == []
    assert complete.calls == []  # never reached the network edge


def test_empty_query_short_circuits(env):
    complete = _fake_complete("unused")
    assert x_grok.fetch_x_articles("   ", complete=complete) == []
    assert complete.calls == []


def test_bad_json_returns_empty_not_raises(env):
    out = x_grok.fetch_x_articles("topic", complete=_fake_complete("not json"))
    assert out == []


def test_api_exception_returns_empty(env):
    def boom(_payload, _key):
        raise RuntimeError("rate limited")

    assert x_grok.fetch_x_articles("topic", complete=boom) == []


def test_request_targets_x_live_search(env):
    """The request asks xAI Live Search to read X, recent-only, with the key."""
    complete = _fake_complete(_reply(_post("https://x.com/a/status/1")))
    x_grok.fetch_x_articles("topic", complete=complete, model="grok-test")
    call = complete.calls[0]
    assert call["api_key"] == "xai-test-key"
    payload = call["payload"]
    assert payload["model"] == "grok-test"
    sp = payload["search_parameters"]
    assert sp["mode"] == "on"
    assert sp["sources"] == [{"type": "x"}]
    assert "from_date" in sp  # recent=True by default


def test_model_defaults_to_configured(env):
    complete = _fake_complete(_reply(_post("https://x.com/a/status/1")))
    x_grok.fetch_x_articles("topic", complete=complete)
    assert complete.calls[0]["payload"]["model"] == settings().xai_model
