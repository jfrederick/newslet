"""Tests for :mod:`newslet.discovery`.

The fake client mimics the subset of :class:`anthropic.Anthropic` that
:func:`newslet.discovery.find_discoveries` uses, so no env/network/API key
is needed.  The web search tool returns multiple content blocks in reality;
the function must read the LAST text block, which these fakes exercise.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from newslet.discovery import find_discoveries


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


# ---------- fixtures ----------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_settings(monkeypatch):
    """Make settings() succeed even though we always pass client=fake.

    find_discoveries() reads ``settings().claude_model``, so the env must
    be populated even when an explicit client is provided.
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


# ---------- tests -------------------------------------------------------


def test_happy_path_returns_two_discoveries():
    payload = {
        "discoveries": [
            {
                "url": "https://newsite.com/a",
                "title": "A",
                "source": "NewSite",
                "reason": "Matches the user's interests.",
            },
            {
                "url": "https://another.org/b",
                "title": "B",
                "source": "Another",
                "reason": "Relevant to the profile.",
            },
        ]
    }
    fake = FakeClient(_text_only(json.dumps(payload)))

    result = find_discoveries("my profile", ["known.com"], client=fake)

    assert len(result) == 2
    assert str(result[0].url) == "https://newsite.com/a"
    assert result[0].reason == "Matches the user's interests."
    # The web search tool must be enabled on the request.
    tools = fake.calls[0]["tools"]
    assert tools[0]["name"] == "web_search"


def test_excludes_url_in_feed_domains():
    payload = {
        "discoveries": [
            {
                "url": "https://www.known.com/already",
                "title": "Followed",
                "source": "Known",
                "reason": "User already follows this.",
            },
            {
                "url": "https://fresh.io/new",
                "title": "Fresh",
                "source": "Fresh",
                "reason": "New to the user.",
            },
        ]
    }
    fake = FakeClient(_text_only(json.dumps(payload)))

    result = find_discoveries("p", ["known.com"], client=fake, max_results=5)

    assert len(result) == 1
    assert str(result[0].url) == "https://fresh.io/new"


def test_reads_last_text_block_amid_tool_blocks():
    """The final text block holds the JSON; earlier blocks are ignored."""
    payload = {
        "discoveries": [
            {
                "url": "https://site.com/x",
                "title": "X",
                "source": "Site",
                "reason": "Fits.",
            }
        ]
    }
    content = [
        SimpleNamespace(type="text", text="Let me search for that."),
        SimpleNamespace(type="server_tool_use", name="web_search"),
        SimpleNamespace(type="web_search_tool_result"),
        SimpleNamespace(type="text", text=json.dumps(payload)),
    ]
    fake = FakeClient(content)

    result = find_discoveries("p", [], client=fake)

    assert len(result) == 1
    assert str(result[0].url) == "https://site.com/x"


def test_malformed_json_returns_empty():
    fake = FakeClient(_text_only("not json at all"))

    result = find_discoveries("p", ["known.com"], client=fake)

    assert result == []


def test_parses_fenced_json():
    """web_search replies often wrap the object in a ```json fence despite
    the 'no fences' instruction; that must still parse, not vanish."""
    payload = {
        "discoveries": [
            {"url": "https://newsite.com/a", "title": "A",
             "source": "NewSite", "reason": "fits"}
        ]
    }
    fenced = "```json\n" + json.dumps(payload) + "\n```"
    fake = FakeClient(_text_only(fenced))

    result = find_discoveries("p", ["known.com"], client=fake)

    assert len(result) == 1
    assert str(result[0].url) == "https://newsite.com/a"


def test_parses_json_with_prose_prefix():
    """A leading sentence before the object must not kill the payload."""
    payload = {
        "discoveries": [
            {"url": "https://fresh.io/y", "title": "Y",
             "source": "Fresh", "reason": "r"}
        ]
    }
    text = "Here are the articles I found:\n\n" + json.dumps(payload)
    fake = FakeClient(_text_only(text))

    result = find_discoveries("p", [], client=fake)

    assert len(result) == 1
    assert str(result[0].url) == "https://fresh.io/y"


def test_parses_json_with_trailing_prose():
    """A clean object followed by a sentence (no fence) must still parse;
    json.loads on the whole string would raise 'Extra data'."""
    payload = {
        "discoveries": [
            {"url": "https://fresh.io/z", "title": "Z",
             "source": "Fresh", "reason": "r"}
        ]
    }
    text = json.dumps(payload) + "\n\nHope that helps!"
    fake = FakeClient(_text_only(text))

    result = find_discoveries("p", [], client=fake)

    assert len(result) == 1
    assert str(result[0].url) == "https://fresh.io/z"


def test_parses_json_with_braces_in_string_values():
    """Braces inside a title/reason must not skew the balanced-brace scan
    on the prose-wrapped path."""
    payload = {
        "discoveries": [
            {"url": "https://fresh.io/g", "title": "How {} works in Go",
             "source": "Fresh", "reason": "covers {tech} topics"}
        ]
    }
    text = "Sure! Here you go:\n" + json.dumps(payload)
    fake = FakeClient(_text_only(text))

    result = find_discoveries("p", [], client=fake)

    assert len(result) == 1
    assert result[0].title == "How {} works in Go"


def test_parses_fenced_json_with_braces_in_string_values():
    """Lock in that fenced content with literal braces in values parses,
    so a future refactor can't regress it."""
    payload = {
        "discoveries": [
            {"url": "https://fresh.io/h", "title": "T",
             "source": "Fresh", "reason": "covers {tech} topics"}
        ]
    }
    fenced = "```json\n" + json.dumps(payload) + "\n```"
    fake = FakeClient(_text_only(fenced))

    result = find_discoveries("p", [], client=fake)

    assert len(result) == 1
    assert result[0].reason == "covers {tech} topics"


def test_picks_json_fence_over_unrelated_fence():
    """When an unrelated fence precedes the JSON fence, the extractor must
    skip the decoy and use the JSON one."""
    payload = {
        "discoveries": [
            {"url": "https://fresh.io/k", "title": "K",
             "source": "Fresh", "reason": "r"}
        ]
    }
    text = (
        "First an example:\n```\nsome example text\n```\n"
        "And the result:\n```json\n" + json.dumps(payload) + "\n```"
    )
    fake = FakeClient(_text_only(text))

    result = find_discoveries("p", [], client=fake)

    assert len(result) == 1
    assert str(result[0].url) == "https://fresh.io/k"


def test_max_results_trims():
    payload = {
        "discoveries": [
            {"url": f"https://s{i}.com/x", "title": f"T{i}",
             "source": "S", "reason": "r"}
            for i in range(5)
        ]
    }
    fake = FakeClient(_text_only(json.dumps(payload)))

    result = find_discoveries("p", [], client=fake, max_results=2)

    assert len(result) == 2
