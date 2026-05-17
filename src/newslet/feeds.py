"""Fetch RSS feeds and filter to fresh, unseen entries.

Pure I/O + filter; no database access. The caller injects an
``is_seen`` callback so dedup state lives outside this module.
"""

from __future__ import annotations

import calendar
import logging
from collections.abc import Callable
from datetime import UTC, datetime

import feedparser
from pydantic import ValidationError

from newslet.contracts import Article

logger = logging.getLogger(__name__)


def _struct_to_utc(struct_time) -> datetime:
    """Convert a feedparser struct_time (assumed UTC) to aware datetime."""
    return datetime.fromtimestamp(calendar.timegm(struct_time), tz=UTC)


def fetch_recent(
    feed_urls: list[str],
    since: datetime,
    is_seen: Callable[[str], bool],
) -> list[Article]:
    """Return articles newer than ``since`` and not yet seen.

    Bad feeds are logged and skipped; no exception escapes for a single
    failing feed.
    """
    results: list[Article] = []

    for feed_url in feed_urls:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as exc:  # noqa: BLE001 - feedparser can raise anything
            logger.warning("Failed to parse feed %s: %s", feed_url, exc)
            continue

        if getattr(parsed, "bozo", 0) and getattr(parsed, "bozo_exception", None):
            logger.warning(
                "Skipping malformed feed %s: %s",
                feed_url,
                parsed.bozo_exception,
            )
            continue

        feed_meta = getattr(parsed, "feed", {}) or {}
        source = feed_meta.get("title", "") or feed_url

        for entry in getattr(parsed, "entries", []) or []:
            link = entry.get("link")
            if not link:
                continue

            published_struct = entry.get("published_parsed") or entry.get(
                "updated_parsed"
            )
            if not published_struct:
                continue

            try:
                published = _struct_to_utc(published_struct)
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "Bad published date on %s in %s: %s", link, feed_url, exc
                )
                continue

            if published < since:
                continue
            if is_seen(link):
                continue

            title = entry.get("title", "") or ""
            summary = entry.get("summary") or entry.get("description") or ""

            try:
                article = Article(
                    url=link,
                    title=title,
                    summary=summary,
                    source=source,
                    published=published,
                )
            except ValidationError as exc:
                logger.warning("Skipping invalid entry %s: %s", link, exc)
                continue

            results.append(article)

    return results
