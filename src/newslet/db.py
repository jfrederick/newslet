"""Thin DynamoDB wrappers for newslet.

All functions read table names lazily from :func:`newslet.config.settings`
so importing this module does not require AWS env vars to be set.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import boto3

from newslet.config import settings
from newslet.contracts import Feed, FeedbackRow, Issue, Pick, Profile

if TYPE_CHECKING:
    pass


_SEEN_TTL_SECONDS = 21 * 86400


def _resource() -> Any:
    return boto3.resource("dynamodb", region_name=settings().aws_region)


def _t_feeds() -> Any:
    return _resource().Table(settings().table_feeds)


def _t_profile() -> Any:
    return _resource().Table(settings().table_profile)


def _t_seen() -> Any:
    return _resource().Table(settings().table_seen)


def _t_issues() -> Any:
    return _resource().Table(settings().table_issues)


def _t_feedback() -> Any:
    return _resource().Table(settings().table_feedback)


def _hash_url(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Feeds
# ---------------------------------------------------------------------------


def list_feeds() -> list[Feed]:
    items = _t_feeds().scan().get("Items", [])
    return [
        Feed(
            url=item["url"],
            title=item.get("title", ""),
            added_at=datetime.fromisoformat(item["added_at"]),
        )
        for item in items
    ]


def add_feed(url: str, title: str = "") -> Feed:
    now = datetime.now(UTC)
    _t_feeds().put_item(
        Item={
            "url": url,
            "title": title,
            "added_at": now.isoformat(),
        }
    )
    return Feed(url=url, title=title, added_at=now)


def delete_feed(url: str) -> None:
    _t_feeds().delete_item(Key={"url": url})


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


def get_profile() -> Profile:
    resp = _t_profile().get_item(Key={"id": "me"})
    item = resp.get("Item")
    if not item:
        return Profile(markdown="", updated_at=datetime.now(UTC))
    return Profile(
        markdown=item.get("markdown", ""),
        updated_at=datetime.fromisoformat(item["updated_at"]),
    )


def put_profile(markdown: str) -> Profile:
    now = datetime.now(UTC)
    _t_profile().put_item(
        Item={
            "id": "me",
            "markdown": markdown,
            "updated_at": now.isoformat(),
        }
    )
    return Profile(markdown=markdown, updated_at=now)


# ---------------------------------------------------------------------------
# Seen articles
# ---------------------------------------------------------------------------


def mark_seen(urls: Iterable[str]) -> None:
    expires_at = int(datetime.now(UTC).timestamp() + _SEEN_TTL_SECONDS)
    table = _t_seen()
    with table.batch_writer() as batch:
        for url in urls:
            batch.put_item(
                Item={
                    "url_hash": _hash_url(url),
                    "url": url,
                    "expires_at": expires_at,
                }
            )


def is_seen(url: str) -> bool:
    resp = _t_seen().get_item(Key={"url_hash": _hash_url(url)})
    return "Item" in resp


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------


def put_issue(issue: Issue) -> None:
    picks_json = json.dumps([json.loads(p.model_dump_json()) for p in issue.picks])
    _t_issues().put_item(
        Item={
            "date": issue.date,
            "picks_json": picks_json,
            "created_at": issue.created_at.isoformat(),
        }
    )


def get_issue(date: str) -> Issue | None:
    resp = _t_issues().get_item(Key={"date": date})
    item = resp.get("Item")
    if not item:
        return None
    picks_raw = json.loads(item["picks_json"])
    picks = [Pick.model_validate(p) for p in picks_raw]
    return Issue.model_validate(
        {
            "date": item["date"],
            "picks": picks,
            "created_at": datetime.fromisoformat(item["created_at"]),
        }
    )


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------


def put_feedback(row: FeedbackRow) -> None:
    _t_feedback().put_item(
        Item={
            "article_url": str(row.article_url),
            "ts": row.ts.isoformat(),
            "title": row.title,
            "rating": row.rating,
        }
    )


def recent_feedback(limit: int = 50) -> list[FeedbackRow]:
    items = _t_feedback().scan(Limit=limit).get("Items", [])
    rows = [
        FeedbackRow(
            article_url=item["article_url"],
            title=item.get("title", ""),
            rating=item["rating"],
            ts=datetime.fromisoformat(item["ts"]),
        )
        for item in items
    ]
    rows.sort(key=lambda r: r.ts, reverse=True)
    return rows
