"""Tests for :mod:`newslet.tune`.

The fake client mimics the subset of :class:`anthropic.Anthropic` that
:func:`newslet.tune.tune_profile` uses, so no env/network/API key is needed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from newslet.contracts import FeedbackRow
from newslet.tune import _BLOCK_END, _BLOCK_START, tune_profile


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
        return SimpleNamespace(content=[SimpleNamespace(text=text)])


# ---------- fixtures ----------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_settings(monkeypatch):
    """Populate the env so settings() succeeds even with an injected client."""
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
def sample_feedback() -> list[FeedbackRow]:
    return [
        FeedbackRow(
            article_url="https://example.com/old1",
            title="Old one",
            rating="up",
            ts=datetime(2026, 1, 1, tzinfo=UTC),
            issue_date="2026-01-01",
            note="more like this",
        ),
        FeedbackRow(
            article_url="https://example.com/old2",
            title="Old two",
            rating="down",
            ts=datetime(2026, 1, 1, tzinfo=UTC),
            issue_date="2026-01-01",
        ),
    ]


HUMAN = "# My profile\n\nI like deep technical writing about distributed systems."


# ---------- tests -------------------------------------------------------


def test_empty_feedback_returns_input_unchanged():
    fake = FakeClient([])
    original = HUMAN + "\n"

    result = tune_profile(original, [], client=fake)

    assert result == original
    assert len(fake.calls) == 0


def test_run_injects_one_block_and_preserves_human(sample_feedback):
    fake = FakeClient(["- likes distributed systems\n- dislikes fluff"])

    result = tune_profile(HUMAN, sample_feedback, client=fake)

    # Exactly one delimited block.
    assert result.count(_BLOCK_START) == 1
    assert result.count(_BLOCK_END) == 1
    # Human text preserved verbatim and still above the block.
    assert result.startswith(HUMAN)
    assert result.index(HUMAN) < result.index(_BLOCK_START)
    # Generated summary made it into the block.
    assert "likes distributed systems" in result
    assert "## Learned preferences (auto)" in result
    # The feedback (rating + note) was sent to Claude.
    sent = fake.calls[0]["messages"][0]["content"]
    assert "more like this" in sent


def test_second_run_replaces_block_no_duplicate(sample_feedback):
    fake = FakeClient([
        "- first summary bullet",
        "- second summary bullet",
    ])

    first = tune_profile(HUMAN, sample_feedback, client=fake)
    second = tune_profile(first, sample_feedback, client=fake)

    # Still exactly one block — replaced, not appended.
    assert second.count(_BLOCK_START) == 1
    assert second.count(_BLOCK_END) == 1
    # New summary present, old one gone.
    assert "second summary bullet" in second
    assert "first summary bullet" not in second
    # Human text still intact and unduplicated.
    assert second.startswith(HUMAN)
    assert second.count("# My profile") == 1


def test_second_run_feeds_existing_block_back_in(sample_feedback):
    """Cumulative: the prior learned summary is sent to Claude on the next run."""
    fake = FakeClient([
        "- likes distributed systems",
        "- updated understanding",
    ])

    first = tune_profile(HUMAN, sample_feedback, client=fake)
    tune_profile(first, sample_feedback, client=fake)

    # The second call's prompt must contain the first run's summary as the
    # "current understanding" being merged, not a from-scratch regeneration.
    second_prompt = fake.calls[1]["messages"][0]["content"]
    assert "likes distributed systems" in second_prompt
    assert "Current learned preferences" in second_prompt


def test_claude_error_returns_input_unchanged(sample_feedback):
    class BoomClient:
        @property
        def messages(self):
            return self

        def create(self, **kw):
            raise RuntimeError("boom")

    original = HUMAN + "\n"
    result = tune_profile(original, sample_feedback, client=BoomClient())

    assert result == original
