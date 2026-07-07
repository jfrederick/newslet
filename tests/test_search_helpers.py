"""Direct unit tests for the shared Claude web-search helpers.

These primitives — the ``web_search`` tool definition, "read the last text
block", "extract the JSON object the model wrapped in prose/fences", the
host-dedup key, and the feed liveness check — are used by
:mod:`newslet.discovery`, :mod:`newslet.websearch`, and
:mod:`newslet.discover`. They were previously only exercised transitively
through ``find_discoveries`` / ``search_web``; this file pins their behavior
directly so the shared home can be refactored with confidence.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from newslet import search_common
from newslet.search_common import (
    extract_json_object,
    feed_is_live,
    host_key,
    last_text_block,
    web_search_tool,
)

# ---------- web_search_tool --------------------------------------------------


def test_web_search_tool_shape():
    tool = web_search_tool(5)
    assert tool["type"] == "web_search_20250305"
    assert tool["name"] == "web_search"
    assert tool["max_uses"] == 5


def test_web_search_tool_floors_max_uses_at_one():
    # The interactive path may pass 0/negative; the tool must still allow at
    # least one search round rather than emit an invalid max_uses.
    assert web_search_tool(0)["max_uses"] == 1
    assert web_search_tool(-3)["max_uses"] == 1


# ---------- last_text_block --------------------------------------------------


def test_last_text_block_picks_final_text_amid_tool_blocks():
    content = [
        SimpleNamespace(type="text", text="Let me search for that."),
        SimpleNamespace(type="server_tool_use", name="web_search"),
        SimpleNamespace(type="web_search_tool_result"),
        SimpleNamespace(type="text", text="FINAL"),
    ]
    assert last_text_block(content) == "FINAL"


def test_last_text_block_none_when_no_text():
    content = [
        SimpleNamespace(type="server_tool_use", name="web_search"),
        SimpleNamespace(type="web_search_tool_result"),
    ]
    assert last_text_block(content) is None


def test_last_text_block_empty_content():
    assert last_text_block([]) is None


# ---------- extract_json_object ---------------------------------------------


def test_extract_plain_object():
    text = '{"articles": []}'
    assert json.loads(extract_json_object(text)) == {"articles": []}


def test_extract_fenced_object():
    payload = {"discoveries": [{"url": "https://x.com/a"}]}
    fenced = "```json\n" + json.dumps(payload) + "\n```"
    assert json.loads(extract_json_object(fenced)) == payload


def test_extract_with_prose_prefix():
    payload = {"k": 1}
    text = "Here are the articles I found:\n\n" + json.dumps(payload)
    assert json.loads(extract_json_object(text)) == payload


def test_extract_with_trailing_prose():
    # A bare json.loads on this whole string raises "Extra data".
    payload = {"k": 2}
    text = json.dumps(payload) + "\n\nHope that helps!"
    assert json.loads(extract_json_object(text)) == payload


def test_extract_ignores_braces_inside_string_values():
    payload = {"title": "How {} works", "reason": "covers {tech} topics"}
    text = "Sure! Here you go:\n" + json.dumps(payload)
    assert json.loads(extract_json_object(text)) == payload


def test_extract_picks_json_fence_over_unrelated_fence():
    payload = {"real": True}
    text = (
        "First an example:\n```\nsome example text\n```\n"
        "And the result:\n```json\n" + json.dumps(payload) + "\n```"
    )
    assert json.loads(extract_json_object(text)) == payload


def test_extract_returns_none_when_no_object():
    assert extract_json_object("not json at all") is None


# ---------- host_key ---------------------------------------------------------


def test_host_key_strips_www_and_lowercases():
    assert host_key("https://WWW.Example.COM/path") == "example.com"


def test_host_key_handles_bare_host_via_scheme_relative():
    # The dedup sets pass "//<domain>" so a bare feed domain resolves a host.
    assert host_key("//Known.com") == "known.com"


def test_host_key_empty_when_no_host():
    assert host_key("not a url") == ""


# ---------- feed_is_live ------------------------------------------------------


def test_feed_is_live_accepts_feed_with_entries(monkeypatch):
    """A parseable feed with entries and no bozo error passes."""
    fake_parsed = SimpleNamespace(bozo=0, bozo_exception=None, entries=[{"x": 1}])
    monkeypatch.setattr(search_common.feedparser, "parse", lambda url: fake_parsed)
    assert feed_is_live("https://ok.com/feed.xml") is True


def test_feed_is_live_rejects_empty_feed(monkeypatch):
    """A well-formed but entry-less feed is not 'live' enough to subscribe."""
    fake_parsed = SimpleNamespace(bozo=0, bozo_exception=None, entries=[])
    monkeypatch.setattr(search_common.feedparser, "parse", lambda url: fake_parsed)
    assert feed_is_live("https://empty.com/feed.xml") is False


def test_feed_is_live_rejects_malformed_feed(monkeypatch):
    """A bozo (fatally malformed) feed is rejected."""
    fake_parsed = SimpleNamespace(
        bozo=1, bozo_exception=Exception("bad xml"), entries=[{"x": 1}]
    )
    monkeypatch.setattr(search_common.feedparser, "parse", lambda url: fake_parsed)
    assert feed_is_live("https://broken.com/feed.xml") is False


def test_feed_is_live_swallows_fetch_error(monkeypatch):
    """A network/parse exception means 'not live', never propagates."""
    def boom(url):
        raise RuntimeError("network down")

    monkeypatch.setattr(search_common.feedparser, "parse", boom)
    assert feed_is_live("https://x.com/feed.xml") is False
