"""Tests for :mod:`newslet.summarize`.

The fake client mimics the subset of :class:`anthropic.Anthropic` that
:func:`newslet.summarize.summarize_issue` uses, so no env/network/API key is
needed.

Error policy under test: any failure to produce/parse a well-formed
``{"subject", "intro"}`` JSON object yields ``("", "")`` without raising.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from newslet.contracts import Pick
from newslet.summarize import summarize_issue


class FakeClient:
    """Stand-in for :class:`anthropic.Anthropic` recording every call."""

    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.calls: list[dict] = []

    @property
    def messages(self):
        return self

    def create(self, **kw):
        self.calls.append(kw)
        text = self._replies.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


# ---------- fixtures ----------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_settings(monkeypatch):
    """Make settings() succeed even though we always pass client=fake.

    summarize_issue() reads ``settings().claude_model`` for the model name,
    so the env must be populated even when an explicit client is provided.
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


@pytest.fixture
def sample_picks() -> list[Pick]:
    return [
        Pick(
            url="https://example.com/1",
            title="Rust adds async closures",
            blurb="The long-awaited feature lands in stable.",
            source="src",
            score=0.9,
        ),
        Pick(
            url="https://example.com/2",
            title="New DuckDB release",
            blurb="Faster joins and a smaller binary.",
            source="src",
            score=0.7,
        ),
    ]


# ---------- tests -------------------------------------------------------


def test_happy_path_parses_subject_and_intro(sample_picks):
    reply = json.dumps(
        {
            "subject": "Rust async closures hit stable",
            "intro": "Two picks today. Rust shipped async closures, and "
            "DuckDB cut its binary size.",
        }
    )
    fake = FakeClient([reply])

    subject, intro = summarize_issue(sample_picks, client=fake)

    assert subject == "Rust async closures hit stable"
    assert intro.startswith("Two picks today.")
    assert len(fake.calls) == 1


def test_malformed_json_returns_empty(sample_picks):
    fake = FakeClient(["not json at all"])

    subject, intro = summarize_issue(sample_picks, client=fake)

    assert (subject, intro) == ("", "")
    assert len(fake.calls) == 1


def test_missing_keys_returns_empty(sample_picks):
    fake = FakeClient([json.dumps({"subject": "only a subject"})])

    assert summarize_issue(sample_picks, client=fake) == ("", "")


def test_non_string_values_return_empty(sample_picks):
    fake = FakeClient([json.dumps({"subject": 42, "intro": ["x"]})])

    assert summarize_issue(sample_picks, client=fake) == ("", "")


def test_system_prompt_has_anti_ai_block(sample_picks):
    reply = json.dumps({"subject": "s", "intro": "i"})
    fake = FakeClient([reply])

    summarize_issue(sample_picks, client=fake)

    system = fake.calls[0]["system"]
    assert "delve" in system
    assert "em-dash" in system.lower()


def test_picks_content_passed_to_model(sample_picks):
    reply = json.dumps({"subject": "s", "intro": "i"})
    fake = FakeClient([reply])

    summarize_issue(sample_picks, client=fake)

    user_content = fake.calls[0]["messages"][0]["content"]
    assert "Rust adds async closures" in user_content
    assert "New DuckDB release" in user_content
