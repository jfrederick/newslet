"""Tests for :mod:`newslet.websearch` with a faked Anthropic client."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from newslet import websearch
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


def _reply(*urls: str) -> str:
    return json.dumps(
        {
            "articles": [
                {"url": u, "title": f"T {i}", "source": "Src", "blurb": "why"}
                for i, u in enumerate(urls)
            ]
        }
    )


def test_parses_articles(env):
    client = _FakeClient(_reply("https://a.example.com/1", "https://b.example.com/2"))
    out = websearch.search_web("quantum computing", client=client)
    assert [str(a.url) for a in out] == [
        "https://a.example.com/1",
        "https://b.example.com/2",
    ]
    assert out[0].source == "Src"


def test_handles_fenced_json_and_prose(env):
    fenced = "Here you go!\n```json\n" + _reply("https://a.example.com/1") + "\n```\nHope it helps."
    out = websearch.search_web("topic", client=_FakeClient(fenced))
    assert [str(a.url) for a in out] == ["https://a.example.com/1"]


def test_excludes_hosts_and_dedupes(env):
    client = _FakeClient(
        _reply(
            "https://known.example.com/x",
            "https://fresh.example.com/y",
            "https://fresh.example.com/y",  # dup
        )
    )
    out = websearch.search_web(
        "topic", client=client, exclude_hosts=["known.example.com"]
    )
    assert [str(a.url) for a in out] == ["https://fresh.example.com/y"]


def test_respects_max_results(env):
    client = _FakeClient(_reply(*[f"https://x{i}.example.com/" for i in range(10)]))
    out = websearch.search_web("topic", client=client, max_results=3)
    assert len(out) == 3


def test_empty_query_short_circuits(env):
    # No client call needed; an empty subject returns nothing.
    assert websearch.search_web("   ", client=_FakeClient("unused")) == []


def test_bad_json_returns_empty_not_raises(env):
    out = websearch.search_web("topic", client=_FakeClient("not json at all"))
    assert out == []


def test_model_and_search_cap_reach_the_api(env):
    """The fast interactive path can override the model and cap tool rounds."""
    client = _FakeClient(_reply("https://a.example.com/1"))
    websearch.search_web(
        "topic", client=client, model="claude-haiku-4-5-20251001", max_searches=2
    )
    call = client.calls[0]
    assert call["model"] == "claude-haiku-4-5-20251001"
    assert call["tools"][0]["max_uses"] == 2


def test_api_exception_returns_empty(env):
    class _Boom:
        @property
        def messages(self):
            return self

        def create(self, **_):
            raise RuntimeError("rate limited")

    assert websearch.search_web("topic", client=_Boom()) == []
