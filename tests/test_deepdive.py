"""Tests for :mod:`newslet.deepdive` with a faked Anthropic client."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from newslet import deepdive
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
        "deepdive": {
            "title": "BGP: the internet's rumor mill",
            "body_md": "Para one.\n\nPara two.",
        }
    }
)


def test_happy_path(env):
    client = _FakeClient(_REPLY)
    dd = deepdive.fetch_deepdive("how does BGP work?", client=client)
    assert dd is not None
    assert dd.topic == "how does BGP work?"
    assert dd.title.startswith("BGP")
    assert "how does BGP work?" in json.dumps(client.calls[0]["messages"])


def test_empty_topic_short_circuits(env):
    # A valid canned reply + call recording makes this bite: without the
    # guard the client would be called and a real DeepDive returned.
    client = _FakeClient(_REPLY)
    assert deepdive.fetch_deepdive("   ", client=client) is None
    assert client.calls == []


def test_api_error_returns_none(env):
    assert deepdive.fetch_deepdive("x", client=_BoomClient()) is None


def test_unparsable_reply_returns_none(env):
    assert deepdive.fetch_deepdive("x", client=_FakeClient("nope")) is None


def test_malformed_reply_returns_none(env):
    assert (
        deepdive.fetch_deepdive("x", client=_FakeClient('{"deepdive": {"title": 1}}'))
        is None
    )


def test_literal_newline_in_body_still_parses(env):
    reply = '{"deepdive": {"title": "T", "body_md": "line one\nline two"}}'
    dd = deepdive.fetch_deepdive("x", client=_FakeClient(reply))
    assert dd is not None
    assert dd.body_md == "line one\nline two"
