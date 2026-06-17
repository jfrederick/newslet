"""Tests for :mod:`newslet.rank`.

The fake client below mimics the subset of :class:`anthropic.Anthropic`
that :func:`newslet.rank.rank` uses, so no env/network/API key is needed.

Error policy under test: when both attempts return unparseable JSON the
function raises :class:`pydantic.ValidationError` (the *original* error
from the first attempt).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from newslet.contracts import Article, FeedbackRow
from newslet.rank import rank


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
    """Make settings() succeed even though we always pass client=fake.

    rank() reads ``settings().claude_model`` to pick the model name, so the
    env must be populated even when an explicit client is provided.
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
def sample_candidates() -> list[Article]:
    return [
        Article(
            url="https://example.com/a",
            title="A",
            summary="sa",
            source="src",
            published=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        Article(
            url="https://example.com/b",
            title="B",
            summary="sb",
            source="src",
            published=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    ]


@pytest.fixture
def sample_feedback() -> list[FeedbackRow]:
    return [
        FeedbackRow(
            article_url="https://example.com/old1",
            title="Old one",
            rating="up",
            ts=datetime(2026, 1, 1, tzinfo=UTC),
            issue_date="2026-01-01",
        ),
        FeedbackRow(
            article_url="https://example.com/old2",
            title="Old two",
            rating="down",
            ts=datetime(2026, 1, 1, tzinfo=UTC),
            issue_date="2026-01-01",
        ),
    ]


def _picks_json(picks: list[dict]) -> str:
    return json.dumps({"picks": picks})


def _candidates(urls: list[str]) -> list[Article]:
    """Build a minimal candidate pool covering the given urls.

    rank() grounds its output to the candidate pool (dropping any pick the
    model invents), so a test asserting non-empty picks must supply candidates
    whose urls match the picks it returns.
    """
    return [
        Article(
            url=u,
            title=u.rsplit("/", 1)[-1],
            summary="s",
            source="src",
            published=datetime(2026, 1, 1, tzinfo=UTC),
        )
        for u in urls
    ]


# ---------- tests -------------------------------------------------------


def test_happy_path_sorted_by_score(sample_feedback):
    picks = [
        {"url": "https://example.com/1", "title": "T1", "blurb": "b1",
         "source": "s", "score": 0.4},
        {"url": "https://example.com/2", "title": "T2", "blurb": "b2",
         "source": "s", "score": 0.9},
        {"url": "https://example.com/3", "title": "T3", "blurb": "b3",
         "source": "s", "score": 0.6},
    ]
    candidates = _candidates([p["url"] for p in picks])
    fake = FakeClient([_picks_json(picks)])

    result = rank(
        "my profile", sample_feedback, candidates,
        client=fake, max_picks=10,
    )

    assert len(result.picks) == 3
    assert [p.score for p in result.picks] == [0.9, 0.6, 0.4]
    assert len(fake.calls) == 1


def test_max_picks_trims_to_top_n(sample_feedback):
    picks = [
        {"url": f"https://example.com/p{i}", "title": f"T{i}",
         "blurb": "b", "source": "s", "score": i / 100.0}
        for i in range(15)
    ]
    candidates = _candidates([p["url"] for p in picks])
    fake = FakeClient([_picks_json(picks)])

    result = rank(
        "p", sample_feedback, candidates,
        client=fake, max_picks=10,
    )

    assert len(result.picks) == 10
    scores = [p.score for p in result.picks]
    assert scores == sorted(scores, reverse=True)
    # Top 10 of scores 0.00..0.14 are 0.05..0.14.
    assert scores[0] == pytest.approx(0.14)
    assert scores[-1] == pytest.approx(0.05)


def test_drops_picks_not_in_candidate_pool(sample_feedback):
    """A pick the model invents (e.g. a stale article it recalls from training)
    is dropped: only urls we actually supplied as candidates survive, so no
    article can bypass the upstream freshness filters."""
    candidates = _candidates(["https://example.com/real"])
    picks = [
        {"url": "https://example.com/real", "title": "Real", "blurb": "b",
         "source": "src", "score": 0.5},
        # Hallucinated — a plausible-looking story that was never a candidate.
        {"url": "https://news.ycombinator.com/item?id=999", "title": "Stale",
         "blurb": "b", "source": "Hacker News", "score": 0.99},
    ]
    fake = FakeClient([_picks_json(picks)])

    result = rank("p", sample_feedback, candidates, client=fake, max_picks=10)

    assert [str(p.url) for p in result.picks] == ["https://example.com/real"]


def test_invalid_json_triggers_retry(sample_feedback):
    candidates = _candidates(["https://example.com/1"])
    good = _picks_json([
        {"url": "https://example.com/1", "title": "T1", "blurb": "b",
         "source": "s", "score": 0.5},
    ])
    fake = FakeClient(["not json", good])

    result = rank(
        "p", sample_feedback, candidates, client=fake, max_picks=10,
    )

    assert len(fake.calls) == 2
    assert len(result.picks) == 1
    # The retry call should include the bad assistant reply + the nudge.
    retry_msgs = fake.calls[1]["messages"]
    assert retry_msgs[-2]["role"] == "assistant"
    assert retry_msgs[-2]["content"] == "not json"
    assert retry_msgs[-1]["role"] == "user"
    assert "ONLY the JSON" in retry_msgs[-1]["content"]


def test_both_attempts_invalid_raises(sample_candidates, sample_feedback):
    fake = FakeClient(["not json", "still not json"])

    with pytest.raises(ValidationError):
        rank("p", sample_feedback, sample_candidates,
             client=fake, max_picks=10)

    assert len(fake.calls) == 2


def test_stable_block_has_cache_control(sample_candidates, sample_feedback):
    good = _picks_json([])
    fake = FakeClient([good])

    rank("my profile", sample_feedback, sample_candidates,
         client=fake, max_picks=10)

    user_content = fake.calls[0]["messages"][0]["content"]
    cached = [b for b in user_content
              if b.get("cache_control") == {"type": "ephemeral"}]
    assert len(cached) == 1
    # The cached block should be the stable (profile + feedback) one.
    assert "profile" in cached[0]["text"].lower()
    assert "feedback" in cached[0]["text"].lower()


def test_system_prompt_mentions_json(sample_candidates, sample_feedback):
    fake = FakeClient([_picks_json([])])

    rank("p", sample_feedback, sample_candidates,
         client=fake, max_picks=10)

    assert "JSON" in fake.calls[0]["system"]


def test_feedback_note_renders_into_stable_block(sample_candidates):
    feedback = [
        FeedbackRow(
            article_url="https://example.com/old1",
            title="Old one",
            rating="up",
            ts=datetime(2026, 1, 1, tzinfo=UTC),
            issue_date="2026-01-01",
            note="too much crypto coverage",
        ),
    ]
    fake = FakeClient([_picks_json([])])

    rank("my profile", feedback, sample_candidates, client=fake, max_picks=10)

    user_content = fake.calls[0]["messages"][0]["content"]
    cached = [b for b in user_content
              if b.get("cache_control") == {"type": "ephemeral"}][0]
    assert "note: too much crypto coverage" in cached["text"]


def test_system_prompt_states_min_picks(sample_candidates, sample_feedback):
    """The default soft floor of 5 picks is communicated to the model."""
    fake = FakeClient([_picks_json([])])

    rank("p", sample_feedback, sample_candidates, client=fake)

    system = fake.calls[0]["system"]
    assert "at least 5" in system
    # The JSON schema braces must survive .format() intact.
    assert '"picks"' in system


def test_output_token_budget_scales_with_max_picks(
    sample_candidates, sample_feedback
):
    """A large pick request (the homepage asks for dozens) gets a bigger
    ``max_tokens`` than a small one (the daily email asks for ~10).

    Regression: a fixed 4096-token budget truncated the homepage's 25-40-pick
    reply into invalid JSON, which failed to parse, failed the retry the same
    way, and raised — aborting the homepage rebuild so it never refreshed.
    """
    small = FakeClient([_picks_json([])])
    rank("p", sample_feedback, sample_candidates, client=small, max_picks=10)
    small_budget = small.calls[0]["max_tokens"]

    large = FakeClient([_picks_json([])])
    rank("p", sample_feedback, sample_candidates, client=large, max_picks=40)
    large_budget = large.calls[0]["max_tokens"]

    assert large_budget > small_budget
    # 40 picks (~300 tokens each) must not be capped at the old fixed 4096.
    assert large_budget >= 40 * 300
    # The retry must use the same generous budget, not fall back to a small one.
    retry = FakeClient(["not json", _picks_json([])])
    rank("p", sample_feedback, _candidates(["https://example.com/1"]),
         client=retry, max_picks=40)
    assert retry.calls[0]["max_tokens"] == retry.calls[1]["max_tokens"]
    assert retry.calls[1]["max_tokens"] >= 40 * 300
