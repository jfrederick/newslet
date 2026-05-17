"""Thin DynamoDB wrappers for newslet.

All functions read table names lazily from :func:`newslet.config.settings`
so importing this module does not require AWS env vars to be set.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from pydantic import HttpUrl, ValidationError

from newslet.config import settings
from newslet.contracts import Feed, FeedbackRow, Issue, Pick, Profile

log = logging.getLogger(__name__)

_SEEN_TTL_SECONDS = 21 * 86400
_FEEDBACK_BUCKET = "all"  # constant PK for the recent-feedback GSI
_FEEDBACK_GSI = "feedback-by-ts"


def normalize_url(url: str) -> str:
    """Validate + normalize a URL via pydantic HttpUrl.

    Raises pydantic ValidationError on invalid input; the caller (web
    layer) translates that to a 400.
    """
    return str(HttpUrl(url))


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
    feeds: list[Feed] = []
    for item in items:
        try:
            feeds.append(
                Feed(
                    url=item["url"],
                    title=item.get("title", ""),
                    added_at=datetime.fromisoformat(item["added_at"]),
                )
            )
        except (ValidationError, KeyError, ValueError) as exc:
            log.warning("skipping bad feed row %r: %s", item.get("url"), exc)
    return feeds


def add_feed(url: str, title: str = "") -> Feed:
    """Validate + normalize the URL before storing.

    Storing the normalized form prevents a class of bugs where the
    primary key in DynamoDB differs from what later reads expose
    through pydantic (e.g., trailing slash, lowercased host) — which
    would cause delete-by-url to silently miss.
    """
    normalized = normalize_url(url)
    now = datetime.now(UTC)
    _t_feeds().put_item(
        Item={
            "url": normalized,
            "title": title,
            "added_at": now.isoformat(),
        }
    )
    return Feed(url=normalized, title=title, added_at=now)


def delete_feed(url: str) -> None:
    """Delete by the normalized URL so it matches what add_feed stored."""
    try:
        normalized = normalize_url(url)
    except ValidationError:
        # Caller passed garbage; nothing to delete.
        return
    _t_feeds().delete_item(Key={"url": normalized})


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
            # Constant bucket attribute so the GSI can return all rows
            # ordered by ts without a full table scan.
            "bucket": _FEEDBACK_BUCKET,
        }
    )


def recent_feedback(limit: int = 50) -> list[FeedbackRow]:
    """Return the ``limit`` most-recent feedback rows, newest first.

    Queries the ``feedback-by-ts`` GSI so we don't pay a full scan as
    the table grows.
    """
    resp = _t_feedback().query(
        IndexName=_FEEDBACK_GSI,
        KeyConditionExpression=Key("bucket").eq(_FEEDBACK_BUCKET),
        ScanIndexForward=False,
        Limit=limit,
    )
    return [
        FeedbackRow(
            article_url=item["article_url"],
            title=item.get("title", ""),
            rating=item["rating"],
            ts=datetime.fromisoformat(item["ts"]),
        )
        for item in resp.get("Items", [])
    ]


def issue_exists(date: str) -> bool:
    """Cheap idempotency check: was an issue already produced for this date?"""
    resp = _t_issues().get_item(
        Key={"date": date},
        ProjectionExpression="#d",
        ExpressionAttributeNames={"#d": "date"},
    )
    return "Item" in resp
