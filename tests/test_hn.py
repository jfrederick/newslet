"""Tests for the Hacker News source (:mod:`newslet.hn`).

The Algolia API is the only network edge; it is injected as a ``fetch``
callable so these stay offline.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from newslet import hn

NOW = datetime(2026, 6, 7, 12, tzinfo=UTC)


def _hit(oid: int, *, points: int, url: str | None = None, comments: int = 0,
         title: str | None = None, story_text: str = "",
         published: datetime | None = None, include_created: bool = True) -> dict:
    hit = {
        "objectID": str(oid),
        "title": title or f"Story {oid}",
        "url": url,
        "points": points,
        "num_comments": comments,
        "author": "tester",
        "story_text": story_text,
    }
    if include_created:
        hit["created_at_i"] = int((published or NOW).timestamp())
    return hit


def _old_hit(oid: int, *, points: int, url: str | None = None) -> dict:
    return _hit(oid, points=points, url=url, published=NOW - timedelta(days=8))


def _fake_fetch(pages: dict[int, list[dict]]):
    """Build a fetch(url) that returns the page named in the query string."""
    def fetch(url: str) -> dict:
        qs = parse_qs(urlsplit(url).query)
        page = int(qs.get("page", ["0"])[0])
        return {"hits": pages.get(page, [])}
    return fetch


def test_fetch_articles_sorts_by_points_and_caps():
    pages = {
        0: [_hit(1, points=10), _hit(2, points=300)],
        1: [_hit(3, points=150)],
    }
    arts = hn.fetch_hn_articles(
        pages=2,
        fetch=_fake_fetch(pages),
        rank_cap=2,
        now=NOW,
    )
    # Highest-points first, capped at 2.
    assert [a.title for a in arts] == ["Story 2", "Story 3"]
    assert all(a.source == "Hacker News" for a in arts)
    # Summary carries usable engagement signal (the whole point of the source).
    assert "300 points" in arts[0].summary


def test_text_post_without_url_falls_back_to_thread():
    pages = {0: [_hit(42, points=50, url=None, story_text="<p>Ask HN: anything?</p>")]}
    arts = hn.fetch_hn_articles(pages=1, fetch=_fake_fetch(pages), now=NOW)
    assert len(arts) == 1
    assert str(arts[0].url) == "https://news.ycombinator.com/item?id=42"
    # HTML stripped into the summary.
    assert "Ask HN: anything?" in arts[0].summary
    assert "<p>" not in arts[0].summary


def test_dedupes_across_pages():
    pages = {
        0: [_hit(1, points=10)],
        1: [_hit(1, points=10), _hit(2, points=5)],
    }
    arts = hn.fetch_hn_articles(pages=2, fetch=_fake_fetch(pages), now=NOW)
    titles = [a.title for a in arts]
    assert titles.count("Story 1") == 1


def test_one_failing_page_does_not_lose_others():
    good = _fake_fetch({0: [_hit(1, points=10)], 1: [_hit(2, points=20)]})

    def flaky(url: str) -> dict:
        if "page=1" in url:
            raise RuntimeError("boom")
        return good(url)

    arts = hn.fetch_hn_articles(pages=3, fetch=flaky, now=NOW)
    # Page 0 survives despite page 1 raising; page 2 is empty -> loop stops.
    assert [a.title for a in arts] == ["Story 1"]


def test_total_failure_returns_empty_not_raises():
    def boom(url: str) -> dict:
        raise RuntimeError("network down")

    assert hn.fetch_hn_articles(pages=2, fetch=boom, now=NOW) == []


def test_fetch_articles_filters_stale_hits_before_rank_cap():
    pages = {
        0: [
            _old_hit(1, points=10_000),
            _hit(2, points=1, published=NOW - timedelta(days=1)),
            _hit(3, points=500, include_created=False),
        ],
    }

    arts = hn.fetch_hn_articles(
        pages=1,
        fetch=_fake_fetch(pages),
        rank_cap=1,
        now=NOW,
    )

    assert [a.title for a in arts] == ["Story 2"]


def test_fetch_articles_requests_recent_algolia_hits():
    seen_urls = []

    def fetch(url: str) -> dict:
        seen_urls.append(url)
        return {"hits": []}

    hn.fetch_hn_articles(pages=1, fetch=fetch, now=NOW)

    qs = parse_qs(urlsplit(seen_urls[0]).query)
    cutoff = int((NOW - timedelta(days=7)).timestamp())
    assert qs["numericFilters"] == [f"created_at_i>={cutoff}"]


def test_fetch_rich_carries_points_comments_and_thread():
    pages = {0: [_hit(7, points=88, comments=42, url="https://ex.com/a")]}
    rich = hn.fetch_hn_rich(pages=1, fetch=_fake_fetch(pages), limit=10, now=NOW)
    assert len(rich) == 1
    r = rich[0]
    assert r.points == 88
    assert r.comments == 42
    assert r.comments_url == "https://news.ycombinator.com/item?id=7"
    assert str(r.url) == "https://ex.com/a"
    assert r.source == "Hacker News"


def test_fetch_rich_filters_stale_hits():
    pages = {
        0: [
            _old_hit(1, points=999),
            _hit(2, points=10, url="https://ex.com/current"),
        ],
    }

    rich = hn.fetch_hn_rich(pages=1, fetch=_fake_fetch(pages), limit=10, now=NOW)

    assert [r.title for r in rich] == ["Story 2"]
