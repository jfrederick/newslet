"""Tests for :mod:`newslet.facts` with a faked Anthropic client."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from newslet import facts
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


def _reply(slots=("mid", "end")) -> str:
    return json.dumps(
        {
            "facts": [
                {
                    "title": f"Fact for {slot}",
                    "body_md": "Paragraph one.\n\nParagraph two.",
                    "genre": "networks & protocols" if slot == "mid" else "algorithms & math",
                    "slot": slot,
                }
                for slot in slots
            ]
        }
    )


def test_happy_path_two_facts(env):
    client = _FakeClient(_reply())
    out = facts.fetch_facts("- likes protocol lore", ["Old topic"], client=client)
    assert [f.slot for f in out] == ["mid", "end"]
    assert out[0].title == "Fact for mid"
    assert out[0].body_md.startswith("Paragraph one.")
    # The profile and the exclusion list both reach the model.
    sent = json.dumps(client.calls[0]["messages"])
    assert "likes protocol lore" in sent
    assert "Old topic" in sent


def test_prose_wrapped_json_still_parses(env):
    client = _FakeClient("Here you go!\n" + _reply() + "\nEnjoy.")
    out = facts.fetch_facts("", [], client=client)
    assert len(out) == 2


def test_api_error_returns_empty(env):
    assert facts.fetch_facts("", [], client=_BoomClient()) == []


def test_unparsable_reply_returns_empty(env):
    assert facts.fetch_facts("", [], client=_FakeClient("no json here")) == []


def test_single_fact_reply_returns_empty(env):
    # A half-result would leave one slot blank — all or nothing.
    assert facts.fetch_facts("", [], client=_FakeClient(_reply(("mid",)))) == []


def test_duplicate_slots_return_empty(env):
    assert facts.fetch_facts("", [], client=_FakeClient(_reply(("mid", "mid")))) == []


def test_malformed_item_returns_empty(env):
    reply = json.dumps(
        {"facts": [{"title": "ok", "body_md": "b", "slot": "mid"}, {"nope": 1}]}
    )
    assert facts.fetch_facts("", [], client=_FakeClient(reply)) == []


def test_tune_facts_profile_empty_feedback_is_noop(env):
    assert facts.tune_facts_profile("- current", [], client=_BoomClient()) == "- current"


def test_tune_facts_profile_error_returns_input(env):
    from datetime import UTC, datetime

    from newslet.contracts import FeedbackRow

    fb = [
        FeedbackRow(
            article_url="https://ex.com/facts/2026-08-06/mid",
            title="Fact",
            rating="up",
            ts=datetime.now(UTC),
            issue_date="2026-08-06",
        )
    ]
    assert facts.tune_facts_profile("- current", fb, client=_BoomClient()) == "- current"


def test_tune_facts_profile_rewrites_markdown(env):
    from datetime import UTC, datetime

    from newslet.contracts import FeedbackRow

    client = _FakeClient("- loves protocol history\n- bored by hardware")
    fb = [
        FeedbackRow(
            article_url="https://ex.com/facts/2026-08-06/mid",
            title="Why TCP has a three-way handshake",
            rating="up",
            ts=datetime.now(UTC),
            issue_date="2026-08-06",
        )
    ]
    out = facts.tune_facts_profile("- old understanding", fb, client=client)
    assert out == "- loves protocol history\n- bored by hardware"
    sent = json.dumps(client.calls[0]["messages"])
    assert "three-way handshake" in sent
    assert "old understanding" in sent


def test_literal_control_chars_in_essay_still_parse(env):
    # A model reply with a real newline inside a JSON string is invalid under
    # strict json.loads; strict=False must accept it rather than costing the
    # reader both fact blocks.
    reply = (
        '{"facts": [\n'
        '{"title": "Mid", "body_md": "line one\nline two", '
        '"genre": "algorithms & math", "slot": "mid"},\n'
        '{"title": "End", "body_md": "b", '
        '"genre": "computing history & lore", "slot": "end"}\n'
        "]}"
    )
    out = facts.fetch_facts("", [], client=_FakeClient(reply))
    assert len(out) == 2
    assert out[0].body_md == "line one\nline two"
