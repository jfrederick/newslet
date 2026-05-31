"""Tests for newslet.feeds.fetch_recent."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from newslet import feeds


def _struct(dt: datetime) -> time.struct_time:
    """Convert an aware datetime to a UTC struct_time (what feedparser yields)."""
    return dt.astimezone(UTC).timetuple()


def _make_parsed(
    *,
    title: str = "Example Feed",
    entries: list[dict] | None = None,
    bozo: int = 0,
    bozo_exception: Exception | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        feed={"title": title},
        entries=entries or [],
        bozo=bozo,
        bozo_exception=bozo_exception,
    )


NOW = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
SINCE = NOW - timedelta(days=1)


def test_all_entries_within_window_returned(monkeypatch):
    parsed = _make_parsed(
        entries=[
            {
                "link": "https://example.com/a",
                "title": "A",
                "summary": "summary a",
                "published_parsed": _struct(NOW - timedelta(hours=1)),
            },
            {
                "link": "https://example.com/b",
                "title": "B",
                "summary": "summary b",
                "published_parsed": _struct(NOW - timedelta(hours=2)),
            },
        ]
    )
    monkeypatch.setattr(feeds.feedparser, "parse", lambda url: parsed)

    out = feeds.fetch_recent(["https://feed.test/rss"], SINCE, lambda _u: False)

    assert len(out) == 2
    assert {str(a.url) for a in out} == {
        "https://example.com/a",
        "https://example.com/b",
    }


def test_old_entries_filtered_out(monkeypatch):
    parsed = _make_parsed(
        entries=[
            {
                "link": "https://example.com/fresh",
                "title": "Fresh",
                "published_parsed": _struct(NOW - timedelta(hours=3)),
            },
            {
                "link": "https://example.com/stale",
                "title": "Stale",
                "published_parsed": _struct(NOW - timedelta(days=5)),
            },
        ]
    )
    monkeypatch.setattr(feeds.feedparser, "parse", lambda url: parsed)

    out = feeds.fetch_recent(["https://feed.test/rss"], SINCE, lambda _u: False)

    assert len(out) == 1
    assert str(out[0].url) == "https://example.com/fresh"


def test_seen_entries_filtered_out(monkeypatch):
    parsed = _make_parsed(
        entries=[
            {
                "link": "https://example.com/new",
                "title": "New",
                "published_parsed": _struct(NOW - timedelta(hours=1)),
            },
            {
                "link": "https://example.com/seen",
                "title": "Seen",
                "published_parsed": _struct(NOW - timedelta(hours=1)),
            },
        ]
    )
    monkeypatch.setattr(feeds.feedparser, "parse", lambda url: parsed)

    seen = {"https://example.com/seen"}
    out = feeds.fetch_recent(
        ["https://feed.test/rss"], SINCE, lambda url: url in seen
    )

    assert len(out) == 1
    assert str(out[0].url) == "https://example.com/new"


def test_failing_feed_does_not_break_others(monkeypatch):
    good = _make_parsed(
        title="Good Feed",
        entries=[
            {
                "link": "https://good.example/a",
                "title": "Good A",
                "published_parsed": _struct(NOW - timedelta(hours=1)),
            }
        ],
    )

    def fake_parse(url):
        if "bad" in url:
            raise RuntimeError("boom")
        return good

    monkeypatch.setattr(feeds.feedparser, "parse", fake_parse)

    out = feeds.fetch_recent(
        ["https://bad.test/rss", "https://good.test/rss"],
        SINCE,
        lambda _u: False,
    )

    assert len(out) == 1
    assert str(out[0].url) == "https://good.example/a"


def test_entry_without_published_date_skipped(monkeypatch):
    parsed = _make_parsed(
        entries=[
            {
                "link": "https://example.com/no-date",
                "title": "No date",
            },
            {
                "link": "https://example.com/dated",
                "title": "Dated",
                "published_parsed": _struct(NOW - timedelta(hours=1)),
            },
        ]
    )
    monkeypatch.setattr(feeds.feedparser, "parse", lambda url: parsed)

    out = feeds.fetch_recent(["https://feed.test/rss"], SINCE, lambda _u: False)

    assert len(out) == 1
    assert str(out[0].url) == "https://example.com/dated"


def test_source_from_feed_title(monkeypatch):
    parsed = _make_parsed(
        title="My Cool Feed",
        entries=[
            {
                "link": "https://example.com/x",
                "title": "X",
                "published_parsed": _struct(NOW - timedelta(hours=1)),
            }
        ],
    )
    monkeypatch.setattr(feeds.feedparser, "parse", lambda url: parsed)

    out = feeds.fetch_recent(["https://feed.test/rss"], SINCE, lambda _u: False)

    assert len(out) == 1
    assert out[0].source == "My Cool Feed"


def test_summary_falls_back_to_description(monkeypatch):
    parsed = _make_parsed(
        entries=[
            {
                "link": "https://example.com/d",
                "title": "D",
                "description": "from description",
                "published_parsed": _struct(NOW - timedelta(hours=1)),
            }
        ]
    )
    monkeypatch.setattr(feeds.feedparser, "parse", lambda url: parsed)

    out = feeds.fetch_recent(["https://feed.test/rss"], SINCE, lambda _u: False)

    assert len(out) == 1
    assert out[0].summary == "from description"


def test_updated_parsed_used_when_published_missing(monkeypatch):
    parsed = _make_parsed(
        entries=[
            {
                "link": "https://example.com/u",
                "title": "U",
                "updated_parsed": _struct(NOW - timedelta(hours=1)),
            }
        ]
    )
    monkeypatch.setattr(feeds.feedparser, "parse", lambda url: parsed)

    out = feeds.fetch_recent(["https://feed.test/rss"], SINCE, lambda _u: False)

    assert len(out) == 1


def test_duplicate_link_across_feeds_deduped(monkeypatch):
    """A syndicated article in two feeds yields exactly one Article."""
    feed_one = _make_parsed(
        title="Feed One",
        entries=[
            {
                "link": "https://example.com/shared",
                "title": "Shared (Feed One)",
                "published_parsed": _struct(NOW - timedelta(hours=1)),
            }
        ],
    )
    feed_two = _make_parsed(
        title="Feed Two",
        entries=[
            {
                "link": "https://example.com/shared",
                "title": "Shared (Feed Two)",
                "published_parsed": _struct(NOW - timedelta(hours=2)),
            }
        ],
    )

    def fake_parse(url):
        return feed_one if "one" in url else feed_two

    monkeypatch.setattr(feeds.feedparser, "parse", fake_parse)

    out = feeds.fetch_recent(
        ["https://feed.one/rss", "https://feed.two/rss"],
        SINCE,
        lambda _u: False,
    )

    assert len(out) == 1
    assert str(out[0].url) == "https://example.com/shared"
    # First occurrence wins (Feed One came first).
    assert out[0].title == "Shared (Feed One)"


def test_bozo_feed_skipped(monkeypatch):
    bad = _make_parsed(
        bozo=1,
        bozo_exception=ValueError("malformed"),
        entries=[
            {
                "link": "https://example.com/x",
                "title": "X",
                "published_parsed": _struct(NOW - timedelta(hours=1)),
            }
        ],
    )
    monkeypatch.setattr(feeds.feedparser, "parse", lambda url: bad)

    out = feeds.fetch_recent(["https://feed.test/rss"], SINCE, lambda _u: False)

    assert out == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
