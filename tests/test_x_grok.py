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
    """Build a complete(payload, api_key) returning a Responses-API reply.

    The Responses output is a list of items; the assistant text lives in a
    ``message`` item's ``output_text`` content block.
    """
    calls: list[dict] = []

    def complete(payload: dict, api_key: str) -> dict:
        calls.append({"payload": payload, "api_key": api_key})
        return {
            "output": [
                {"type": "reasoning", "content": []},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": reply}],
                },
            ]
        }

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


@pytest.mark.parametrize("output", [5, "boom", {"error": "nope"}, None])
def test_malformed_output_returns_empty_not_raises(env, output):
    """A non-list `output` (error object, scalar) degrades to [], never raises."""
    out = x_grok.fetch_x_articles(
        "topic", complete=lambda _p, _k: {"output": output}
    )
    assert out == []


def test_api_exception_returns_empty(env):
    def boom(_payload, _key):
        raise RuntimeError("rate limited")

    assert x_grok.fetch_x_articles("topic", complete=boom) == []


def test_request_uses_x_search_tool_on_responses_api(env):
    """The request uses the Agent Tools x_search tool, recent-only, with key."""
    complete = _fake_complete(_reply(_post("https://x.com/a/status/1")))
    x_grok.fetch_x_articles("topic", complete=complete, model="grok-test")
    call = complete.calls[0]
    assert call["api_key"] == "xai-test-key"
    payload = call["payload"]
    assert payload["model"] == "grok-test"
    # Responses API: `input`, not chat `messages`; no legacy search_parameters.
    assert "input" in payload
    assert "search_parameters" not in payload
    tool = payload["tools"][0]
    assert tool["type"] == "x_search"
    assert "from_date" in tool  # recent=True puts recency in the tool itself
    # A bounded output budget so a runaway reply can't burn tokens.
    assert payload["max_output_tokens"] == x_grok._MAX_OUTPUT_TOKENS


def test_model_defaults_to_configured(env):
    complete = _fake_complete(_reply(_post("https://x.com/a/status/1")))
    x_grok.fetch_x_articles("topic", complete=complete)
    assert complete.calls[0]["payload"]["model"] == settings().xai_model


def test_reads_output_text_aggregate(env):
    """A reply that only carries the convenience output_text aggregate parses."""
    def complete(payload, api_key):
        return {"output_text": _reply(_post("https://x.com/a/status/1"))}

    out = x_grok.fetch_x_articles("topic", complete=complete)
    assert [str(a.url) for a in out] == ["https://x.com/a/status/1"]


def test_recent_false_omits_date_range(env):
    complete = _fake_complete(_reply(_post("https://x.com/a/status/1")))
    x_grok.fetch_x_articles("topic", complete=complete, recent=False)
    assert "from_date" not in complete.calls[0]["payload"]["tools"][0]


def test_fetch_x_posts_returns_post_shape(env):
    """fetch_x_posts keeps the display fields (author/likes/reposts/text)."""
    complete = _fake_complete(
        _reply(_post("https://x.com/a/status/1", author="@alice",
                     text="Big news", likes=980, reposts=240))
    )
    out = x_grok.fetch_x_posts("AI policy", complete=complete)
    assert len(out) == 1
    post = out[0]
    assert str(post.url) == "https://x.com/a/status/1"
    assert post.title == "Big news"
    assert post.text == "Big news"
    assert post.author == "alice"  # leading @ stripped
    assert post.likes == 980
    assert post.reposts == 240


def test_fetch_x_posts_tolerates_missing_engagement(env):
    complete = _fake_complete(
        _reply({"url": "https://x.com/a/status/1", "text": "t"})
    )
    out = x_grok.fetch_x_posts("topic", complete=complete)
    assert out[0].likes is None
    assert out[0].reposts is None


def test_fetch_x_posts_truncates_long_text_into_title(env):
    text = "word " * 40
    complete = _fake_complete(
        _reply(_post("https://x.com/a/status/1", text=text.strip()))
    )
    out = x_grok.fetch_x_posts("topic", complete=complete)
    assert out[0].title.endswith("…")
    assert len(out[0].title) == 101
    assert out[0].text == text.strip()


def test_as_articles_converts_for_the_ranking_pool(env):
    complete = _fake_complete(
        _reply(_post("https://x.com/a/status/1", author="@alice",
                     text="Big news", likes=100, reposts=10))
    )
    posts = x_grok.fetch_x_posts("topic", complete=complete)
    arts = x_grok.as_articles(posts)
    assert len(arts) == 1
    assert arts[0].source == "X"
    assert arts[0].title == "Big news"
    assert "100 likes, 10 reposts" in arts[0].summary
    assert "@alice" in arts[0].summary
