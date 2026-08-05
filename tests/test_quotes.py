"""Tests for :mod:`newslet.quotes` with a faked Anthropic client."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from newslet import quotes
from newslet.config import settings
from newslet.contracts import FeedbackRow


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
        self.calls: list[dict] = []

    @property
    def messages(self):
        return self

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self._reply)])


class _BoomClient:
    @property
    def messages(self):
        return self

    def create(self, **_):
        raise RuntimeError("api down")


_REPLY = json.dumps(
    {
        "quote": {
            "text": "The impediment to action advances action.",
            "author": "Marcus Aurelius",
            "source": "Meditations",
            "tradition": "Stoic",
        }
    }
)


def _fb(title: str = "Quote: Marcus Aurelius") -> FeedbackRow:
    return FeedbackRow(
        article_url="https://ex.com/quote/2026-08-08",
        title=title,
        rating="up",
        ts=datetime.now(UTC),
        issue_date="2026-08-08",
    )


def test_happy_path(env):
    client = _FakeClient(_REPLY)
    q = quotes.fetch_quote("- likes Stoics", ["Seneca — On anger"], client=client)
    assert q is not None
    assert q.author == "Marcus Aurelius"
    assert q.tradition == "Stoic"
    sent = json.dumps(client.calls[0]["messages"])
    assert "likes Stoics" in sent
    assert "On anger" in sent
    # Default model is the fast one — quotes run daily and are short.
    assert client.calls[0]["model"] == quotes._QUOTE_MODEL


def test_prose_wrapped_json_still_parses(env):
    q = quotes.fetch_quote("", [], client=_FakeClient("Sure!\n" + _REPLY))
    assert q is not None


def test_api_error_returns_none(env):
    assert quotes.fetch_quote("", [], client=_BoomClient()) is None


def test_unparsable_reply_returns_none(env):
    assert quotes.fetch_quote("", [], client=_FakeClient("nope")) is None


def test_malformed_quote_returns_none(env):
    reply = json.dumps({"quote": {"author": "Nobody"}})  # no text
    assert quotes.fetch_quote("", [], client=_FakeClient(reply)) is None


def test_vote_path_shape():
    assert quotes.is_quote_vote("/quote/2026-08-08")
    assert quotes.is_quote_vote("/quote/manual-20260808-042944-7c43c81f")
    assert not quotes.is_quote_vote("/quote/stoicism")
    assert not quotes.is_quote_vote("/blog/quote/2026-08-08")
    assert not quotes.is_quote_vote("/quote/2026-08-08/extra")


def test_tune_quotes_profile_empty_feedback_is_noop(env):
    assert quotes.tune_quotes_profile("- cur", [], client=_BoomClient()) == "- cur"


def test_tune_quotes_profile_error_returns_input(env):
    assert quotes.tune_quotes_profile("- cur", [_fb()], client=_BoomClient()) == "- cur"


def test_tune_quotes_profile_rewrites_markdown(env):
    client = _FakeClient("- loves Stoics\n- lukewarm on Nietzsche")
    out = quotes.tune_quotes_profile("- old", [_fb()], client=client)
    assert out == "- loves Stoics\n- lukewarm on Nietzsche"
    sent = json.dumps(client.calls[0]["messages"])
    assert "Marcus Aurelius" in sent
    assert "old" in sent
