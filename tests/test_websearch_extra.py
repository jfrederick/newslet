"""Additional tests for :mod:`newslet.websearch` — covering variety directives,
no-text-block response, JSONDecodeError, and ValidationError paths.
"""

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
    def __init__(self, reply: str):
        self._reply = reply

    @property
    def messages(self):
        return self

    def create(self, **kw):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self._reply)])


class _NoTextClient:
    """Returns a response with no text blocks (e.g., only tool_use blocks)."""

    @property
    def messages(self):
        return self

    def create(self, **kw):
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", text=None)])


# --- _variety_directive coverage ---


def test_variety_directive_low():
    result = websearch._variety_directive(10)
    assert "tightly" in result


def test_variety_directive_medium_low():
    result = websearch._variety_directive(30)
    assert "adjacent" in result


def test_variety_directive_medium_high():
    result = websearch._variety_directive(60)
    assert "Balance" in result or "ancillary" in result


def test_variety_directive_high():
    result = websearch._variety_directive(90)
    assert "Emphasize" in result


def test_variety_directive_clamps_negatives():
    result = websearch._variety_directive(-5)
    assert "tightly" in result


def test_variety_directive_clamps_over_100():
    result = websearch._variety_directive(200)
    assert "Emphasize" in result


# --- _build_user_block coverage ---


def test_build_user_block_with_recency():
    result = websearch._build_user_block("my query", recent=True, variety=50)
    assert "last week" in result
    assert "my query" in result


def test_build_user_block_without_recency():
    result = websearch._build_user_block("my query", recent=False, variety=50)
    assert "last week" not in result
    assert "my query" in result


# --- no text block in response ---


def test_no_text_block_returns_empty(env):
    out = websearch.search_web("topic", client=_NoTextClient())
    assert out == []


# --- invalid JSON response ---


def test_parseable_but_no_articles_key_returns_empty(env):
    client = _FakeClient(json.dumps({"not_articles": []}))
    out = websearch.search_web("topic", client=client)
    assert out == []


# --- malformed article items (ValidationError) ---


def test_malformed_article_item_dropped(env):
    payload = json.dumps({
        "articles": [
            {"url": "https://valid.example.com/1", "title": "Good",
             "source": "S", "blurb": "b"},
            {"bad_key": "no url here"},
            {"url": "https://valid.example.com/2", "title": "Also Good",
             "source": "S", "blurb": "b"},
        ]
    })
    out = websearch.search_web("topic", client=_FakeClient(payload))
    assert len(out) == 2
    assert str(out[0].url) == "https://valid.example.com/1"
    assert str(out[1].url) == "https://valid.example.com/2"


# --- variety passed through to the API call ---


def test_variety_reaches_system_prompt(env):
    class _CapturingClient:
        def __init__(self):
            self.calls = []

        @property
        def messages(self):
            return self

        def create(self, **kw):
            self.calls.append(kw)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text='{"articles": []}')]
            )

    client = _CapturingClient()
    websearch.search_web("topic", client=client, variety=80)
    msg = client.calls[0]["messages"][0]["content"]
    assert "Emphasize" in msg or "ancillary" in msg
