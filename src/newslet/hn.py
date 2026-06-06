"""Hacker News as a first-class source with *usable* content.

The public HN RSS feed (``hnrss.org/frontpage``) carries little more than a
title and a link, which gives the ranker almost nothing to judge relevance
on. This module instead pulls from the Algolia HN Search API, which returns
structured records — points, comment count, author, and (for text posts) the
body — so each story arrives with enough signal to rank well and to render
richly in the web view.

Two shapes come out of here:

- :func:`fetch_hn_articles` → ``list[Article]`` to merge into the digest's
  ranking candidate pool (so HN stories compete with RSS for the daily picks).
- :func:`fetch_hn_rich` → ``list[WebArticle]`` for the web view's live
  "Hacker News" panel, carrying ``points``/``comments`` and a link to the
  discussion thread.

All network access goes through an injected ``fetch`` callable so tests stay
offline. Every fetch is best-effort: a failed page is logged and skipped, and
a total failure yields an empty list rather than raising — HN must never block
a digest.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.request import Request, urlopen

from pydantic import ValidationError

from .contracts import Article, WebArticle

logger = logging.getLogger(__name__)

# Algolia's HN Search API. ``tags=story`` over the relevance-ranked search
# index approximates "the HN front pages": page 0 is the current front page,
# and successive pages walk down the ranking. 30 hits/page mirrors HN's own
# page size, so 20 pages ≈ the first 20 front pages.
_ALGOLIA_SEARCH = "https://hn.algolia.com/api/v1/search"
_HITS_PER_PAGE = 30
_DEFAULT_PAGES = 20
_REQUEST_TIMEOUT = 8
_USER_AGENT = "newslet/1.0 (+https://github.com/jfrederick/newslet)"

# Keep the ranking prompt tractable: 20 pages is up to 600 stories, which
# would balloon the rank call. Fetch all of them (the user asked for "at
# least the first 20 pages"), but pass only the highest-signal subset on to
# the ranker, ordered by points.
_DEFAULT_RANK_CAP = 120

_TAG_RE = re.compile(r"<[^>]+>")


def _default_fetch(url: str) -> dict:
    """Fetch ``url`` and parse it as JSON. Used in production; tests inject."""
    req = Request(url, headers={"User-Agent": _USER_AGENT})
    with urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:  # noqa: S310 - https only
        return json.loads(resp.read().decode("utf-8"))


def _strip_html(text: str) -> str:
    """Collapse Algolia's HTML ``story_text`` into a plain one-liner."""
    no_tags = _TAG_RE.sub(" ", text or "")
    unescaped = (
        no_tags.replace("&#x27;", "'")
        .replace("&quot;", '"')
        .replace("&amp;", "&")
        .replace("&gt;", ">")
        .replace("&lt;", "<")
    )
    return " ".join(unescaped.split())


def _comments_url(object_id: str) -> str:
    return f"https://news.ycombinator.com/item?id={object_id}"


def _hit_url(hit: dict) -> str | None:
    """The canonical link for a hit.

    Link posts carry an external ``url``; Ask/Show/text posts have none, so
    fall back to the HN discussion thread (still a real, rankable page).
    """
    url = hit.get("url")
    object_id = hit.get("objectID")
    if url:
        return url
    if object_id:
        return _comments_url(str(object_id))
    return None


def _fetch_hits(
    pages: int,
    fetch: Callable[[str], dict],
    *,
    tags: str = "story",
) -> list[dict]:
    """Walk ``pages`` of the Algolia search index, deduped by objectID.

    A single failing page is logged and skipped so a transient error on
    page 7 doesn't lose pages 0–6.
    """
    seen: set[str] = set()
    hits: list[dict] = []
    for page in range(max(pages, 0)):
        url = f"{_ALGOLIA_SEARCH}?tags={tags}&page={page}&hitsPerPage={_HITS_PER_PAGE}"
        try:
            payload = fetch(url)
        except Exception as exc:  # noqa: BLE001 - any network/parse error is non-fatal
            logger.warning("hn: page %d fetch failed: %s", page, exc)
            continue
        page_hits = payload.get("hits", []) if isinstance(payload, dict) else []
        if not page_hits:
            # Ran past the last available page; stop early.
            break
        for hit in page_hits:
            oid = str(hit.get("objectID", ""))
            if oid and oid in seen:
                continue
            seen.add(oid)
            hits.append(hit)
    return hits


def _summary_for(hit: dict) -> str:
    """A ranking-useful one-liner: engagement signal plus any body snippet."""
    points = hit.get("points") or 0
    comments = hit.get("num_comments") or 0
    author = hit.get("author") or "?"
    head = f"{points} points, {comments} comments on Hacker News (by {author})."
    body = _strip_html(hit.get("story_text", ""))
    if body:
        head += " " + (body[:240] + "…" if len(body) > 240 else body)
    return head


def fetch_hn_articles(
    pages: int = _DEFAULT_PAGES,
    *,
    fetch: Callable[[str], dict] | None = None,
    rank_cap: int = _DEFAULT_RANK_CAP,
) -> list[Article]:
    """Return HN stories as ranking candidates, best-effort.

    Fetches ``pages`` pages (the user asked for "at least the first 20"),
    then keeps the highest-points ``rank_cap`` as :class:`Article`\\ s with a
    content-rich ``summary`` so the ranker can actually judge them. Returns
    ``[]`` on any failure — HN must never break the digest.
    """
    fetch = fetch or _default_fetch
    try:
        hits = _fetch_hits(pages, fetch)
    except Exception:  # noqa: BLE001 - belt-and-suspenders; never raise out of here
        logger.exception("hn: fetch failed; skipping HN source")
        return []

    hits.sort(key=lambda h: h.get("points") or 0, reverse=True)

    articles: list[Article] = []
    for hit in hits[: max(rank_cap, 0)]:
        url = _hit_url(hit)
        if not url:
            continue
        created = hit.get("created_at_i")
        published = (
            datetime.fromtimestamp(created, tz=UTC)
            if isinstance(created, (int, float))
            else datetime.now(UTC)
        )
        try:
            articles.append(
                Article(
                    url=url,
                    title=hit.get("title") or "(untitled)",
                    summary=_summary_for(hit),
                    source="Hacker News",
                    published=published,
                )
            )
        except ValidationError as exc:
            logger.info("hn: skipping unrankable hit %s: %s", hit.get("objectID"), exc)
    return articles


def fetch_hn_rich(
    pages: int = 2,
    *,
    fetch: Callable[[str], dict] | None = None,
    limit: int = 20,
) -> list[WebArticle]:
    """Return the top HN stories as :class:`WebArticle`\\ s for the web view.

    Carries ``points``/``comments`` and a ``comments_url`` so the live
    "Hacker News" panel can show engagement and link to the discussion.
    Best-effort: ``[]`` on any failure.
    """
    fetch = fetch or _default_fetch
    try:
        hits = _fetch_hits(pages, fetch, tags="front_page")
    except Exception:  # noqa: BLE001 - never raise out of a best-effort fetch
        logger.exception("hn: rich fetch failed")
        return []

    # front_page is already in HN's ranked order; fall back to points if the
    # tag ever changes shape under us.
    if not hits:
        return []
    hits = hits[: max(limit, 0)]

    out: list[WebArticle] = []
    for hit in hits:
        url = _hit_url(hit)
        if not url:
            continue
        oid = str(hit.get("objectID", ""))
        try:
            out.append(
                WebArticle(
                    url=url,
                    title=hit.get("title") or "(untitled)",
                    blurb=_strip_html(hit.get("story_text", ""))[:200],
                    source="Hacker News",
                    points=hit.get("points"),
                    comments=hit.get("num_comments"),
                    comments_url=_comments_url(oid) if oid else "",
                )
            )
        except ValidationError as exc:
            logger.info("hn: skipping rich hit %s: %s", oid, exc)
    return out
