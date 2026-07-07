"""Tests for :mod:`newslet.serendipity` with a faked Anthropic client."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from newslet import serendipity
from newslet.config import settings


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("RESEND_API_KEY", "x")
    monkeypatch.setenv("FROM_EMAIL", "f@example.com")
    monkeypatch.setenv("TO_EMAIL", "t@example.com")
    monkeypatch.setenv("ADMIN_TOKEN", "x")
    monkeypatch.setenv("SIGNING_KEY", "k")
    settings.cache_clear()
    yield
    settings.cache_clear()


class _FakeClient:
    """Minimal stand-in: returns a single text block with ``reply``."""

    def __init__(self, reply: str):
        self._reply = reply
        self.calls: list[dict] = []

    @property
    def messages(self):
        return self

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self._reply)])


class _BoomIfCalled:
    """Raises if ``create`` is ever invoked — used to prove short-circuits."""

    @property
    def messages(self):
        return self

    def create(self, **_):
        raise AssertionError("client.messages.create should not have been called")


def _reply(*urls: str) -> str:
    return json.dumps(
        {
            "articles": [
                {"url": u, "title": f"T {i}", "source": "Src", "blurb": "topic"}
                for i, u in enumerate(urls)
            ]
        }
    )


def test_parses_articles_happy_path(env):
    client = _FakeClient(
        _reply(
            "https://a.example.com/1",
            "https://b.example.com/2",
            "https://c.example.com/3",
        )
    )
    out = serendipity.fetch_serendipity("## Interests\nBirdwatching, jazz", client=client)
    assert [str(a.url) for a in out] == [
        "https://a.example.com/1",
        "https://b.example.com/2",
        "https://c.example.com/3",
    ]
    assert out[0].source == "Src"


def test_caps_at_max_results_and_dedupes(env):
    client = _FakeClient(
        _reply(
            "https://a.example.com/1",
            "https://a.example.com/1",  # dup
            "https://b.example.com/2",
            "https://c.example.com/3",
            "https://d.example.com/4",
        )
    )
    out = serendipity.fetch_serendipity("profile", client=client, max_results=2)
    assert [str(a.url) for a in out] == [
        "https://a.example.com/1",
        "https://b.example.com/2",
    ]


def test_max_results_zero_short_circuits(env):
    out = serendipity.fetch_serendipity("profile", client=_BoomIfCalled(), max_results=0)
    assert out == []


def test_malformed_item_is_dropped_others_kept(env):
    payload = json.dumps(
        {
            "articles": [
                {"title": "no url here", "source": "Src", "blurb": "topic"},
                {
                    "url": "https://good.example.com/1",
                    "title": "T",
                    "source": "Src",
                    "blurb": "topic",
                },
            ]
        }
    )
    out = serendipity.fetch_serendipity("profile", client=_FakeClient(payload))
    assert [str(a.url) for a in out] == ["https://good.example.com/1"]


def test_no_json_in_reply_returns_empty(env):
    out = serendipity.fetch_serendipity("profile", client=_FakeClient("just some prose, no json"))
    assert out == []


def test_api_exception_returns_empty_never_raises(env):
    class _Boom:
        @property
        def messages(self):
            return self

        def create(self, **_):
            raise RuntimeError("rate limited")

    out = serendipity.fetch_serendipity("profile", client=_Boom())
    assert out == []


def test_system_prompt_excludes_tech_and_states_recency(env):
    client = _FakeClient(_reply("https://a.example.com/1"))
    serendipity.fetch_serendipity("profile", client=client, max_results=3, max_searches=2)
    call = client.calls[0]
    system = call["system"]
    assert "artificial intelligence" in system
    assert "exclu" in system.lower()  # "exclusion" / "exclude"
    assert "PAST WEEK" in system
    assert call["tools"][0]["max_uses"] == 2
    assert "3" in system  # max_results interpolated


def test_empty_profile_uses_generic_fallback(env):
    client = _FakeClient(_reply("https://a.example.com/1"))
    out = serendipity.fetch_serendipity("   ", client=client)
    assert [str(a.url) for a in out] == ["https://a.example.com/1"]
    user_content = client.calls[0]["messages"][0]["content"]
    assert "curious generalist reader" in user_content
