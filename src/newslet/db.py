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
from newslet.contracts import (
    Article,
    Config,
    Discovery,
    Feed,
    FeedbackRow,
    Issue,
    Pick,
    Profile,
    Subscription,
    WebArticle,
)

log = logging.getLogger(__name__)

_SEEN_TTL_SECONDS = 21 * 86400
_FEEDBACK_GSI = "feedback-by-ts"
# Received-newsletter rows expire after 30d — comfortably past the 24h digest
# window plus any retry/backfill, while keeping the table from growing forever.
_INBOX_TTL_SECONDS = 30 * 86400
_INBOX_GSI = "inbox-by-ts"


def _feedback_bucket(ts: datetime) -> str:
    """Shard key for the recent-feedback GSI — one bucket per year.

    Personal-app traffic comfortably fits in a single per-year partition;
    sharding avoids the documented hot-partition anti-pattern from a
    constant string PK without the complexity of monthly shards.
    """
    return ts.strftime("%Y")


def _inbox_bucket(ts: datetime) -> str:
    """Per-year shard for the recent-inbox GSI — same rationale as feedback."""
    return ts.strftime("%Y")


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


def _t_subscriptions() -> Any:
    return _resource().Table(settings().table_subscriptions)


def _t_inbox() -> Any:
    return _resource().Table(settings().table_inbox)


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
# Config (admin knobs) — shares the profile table under a distinct id
# ---------------------------------------------------------------------------


def get_config() -> Config:
    """Return the admin config, or sensible defaults if unset/legacy.

    Lenient on read: a missing row or an out-of-range/garbled value falls
    back to defaults rather than raising, so the daily send is never blocked
    by a bad config row.
    """
    resp = _t_profile().get_item(Key={"id": "config"})
    item = resp.get("Item")
    if not item:
        return Config()
    try:
        return Config(
            max_rss_articles=int(item.get("max_rss_articles", 10)),
            max_web_articles=int(item.get("max_web_articles", 5)),
            web_variety=int(item.get("web_variety", 30)),
        )
    except (ValidationError, ValueError, TypeError) as exc:
        log.warning("bad config row, using defaults: %s", exc)
        return Config()


def put_config(config: Config) -> Config:
    """Persist the admin config (validated by the Config model first)."""
    _t_profile().put_item(
        Item={
            "id": "config",
            "max_rss_articles": config.max_rss_articles,
            "max_web_articles": config.max_web_articles,
            "web_variety": config.web_variety,
            "updated_at": datetime.now(UTC).isoformat(),
        }
    )
    return config


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


def put_issue(issue: Issue, *, manual: bool = False) -> None:
    picks_json = json.dumps([json.loads(p.model_dump_json()) for p in issue.picks])
    discoveries_json = json.dumps(
        [json.loads(d.model_dump_json()) for d in issue.discoveries]
    )
    web_articles_json = json.dumps(
        [json.loads(w.model_dump_json()) for w in issue.web_articles]
    )
    item: dict[str, Any] = {
        "date": issue.date,
        "picks_json": picks_json,
        "created_at": issue.created_at.isoformat(),
        # Persist the enrichment fields so a retry that reuses the stored
        # issue re-sends with the same subject/intro/discoveries, and the
        # web view re-renders the same web_articles block.
        "subject": issue.subject,
        "intro": issue.intro,
        "discoveries_json": discoveries_json,
        "web_articles_json": web_articles_json,
    }
    if manual:
        # Manual ("send now") issues are stored so /rate title lookup and
        # issue viewing still work, but flagged so list_issues hides them
        # from "recent issues" — see list_issues.
        item["manual"] = True
    _t_issues().put_item(Item=item)


def get_issue(date: str) -> Issue | None:
    resp = _t_issues().get_item(Key={"date": date})
    item = resp.get("Item")
    if not item:
        return None
    picks_raw = json.loads(item["picks_json"])
    picks = [Pick.model_validate(p) for p in picks_raw]
    # Validate discoveries leniently: issues persisted before feed_url became
    # required (added for one-click subscribe) have discovery rows without it,
    # and a strict validate here would raise and make the whole issue
    # unreadable — breaking /rate and /emails/{date} for every old issue that
    # had discoveries. Skip the unrenderable ones instead; picks and the
    # rating links (the load-bearing part of an archived issue) still resolve.
    discoveries_raw = json.loads(item.get("discoveries_json", "[]"))
    discoveries = []
    for d in discoveries_raw:
        try:
            discoveries.append(Discovery.model_validate(d))
        except ValidationError as exc:
            log.warning(
                "skipping legacy discovery without feed_url in issue %s: %s",
                item.get("date"),
                exc,
            )
    # web_articles is optional and was added after the first issues were
    # written; validate leniently so an old row (no field) or a single bad
    # entry never makes the whole issue unreadable.
    web_articles_raw = json.loads(item.get("web_articles_json", "[]"))
    web_articles = []
    for w in web_articles_raw:
        try:
            web_articles.append(WebArticle.model_validate(w))
        except ValidationError as exc:
            log.warning(
                "skipping bad web_article in issue %s: %s", item.get("date"), exc
            )
    return Issue.model_validate(
        {
            "date": item["date"],
            "picks": picks,
            "created_at": datetime.fromisoformat(item["created_at"]),
            "subject": item.get("subject", ""),
            "intro": item.get("intro", ""),
            "discoveries": discoveries,
            "web_articles": web_articles,
        }
    )


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------


def put_feedback(row: FeedbackRow) -> None:
    """Store a +/- click.

    Primary key is ``(article_url, issue_date)`` so clicking the same
    article again in the same issue overwrites the previous vote —
    avoiding contradictory pairs of `+` and `-` rows that would confuse
    Claude on the next run.
    """
    _t_feedback().put_item(
        Item={
            "article_url": str(row.article_url),
            "issue_date": row.issue_date,
            "ts": row.ts.isoformat(),
            "title": row.title,
            "rating": row.rating,
            "note": row.note,
            "bucket": _feedback_bucket(row.ts),
        }
    )


def update_feedback_note(article_url: str, issue_date: str, note: str) -> None:
    """Update only the ``note`` attribute on an existing feedback row.

    Keyed by ``(article_url, issue_date)``; leaves the rating/ts/title
    untouched so a free-text note can be attached after the initial
    +/- click without overwriting the vote.
    """
    # ``note`` is a DynamoDB reserved word, so it must be aliased via
    # ExpressionAttributeNames inside the UpdateExpression.
    _t_feedback().update_item(
        Key={"article_url": article_url, "issue_date": issue_date},
        UpdateExpression="SET #n = :n",
        ExpressionAttributeNames={"#n": "note"},
        ExpressionAttributeValues={":n": note},
    )


def feedback_ratings(urls: list[str], issue_date: str) -> dict[str, str]:
    """Return ``{article_url: rating}`` for the given urls in one issue.

    Powers the web view's "sticky" vote state — showing which articles you
    already voted on so the effect of a +/- is visible. Uses ``batch_get_item``
    (≤100 keys per call) on the ``(article_url, issue_date)`` PK, so it is an
    exact point-read per article rather than a table scan. Best-effort and
    lenient: any read hiccup yields a partial/empty map, never an exception.
    """
    if not urls:
        return {}
    keys = [{"article_url": u, "issue_date": issue_date} for u in dict.fromkeys(urls)]
    table_name = settings().table_feedback
    resource = _resource()
    ratings: dict[str, str] = {}
    # batch_get_item caps at 100 keys; chunk to stay under it.
    for start in range(0, len(keys), 100):
        chunk = keys[start : start + 100]
        try:
            resp = resource.batch_get_item(
                RequestItems={
                    table_name: {
                        "Keys": chunk,
                        "ProjectionExpression": "article_url, rating",
                    }
                }
            )
        except Exception as exc:  # noqa: BLE001 - vote state is decorative; never fail the page
            log.warning("feedback_ratings batch_get failed: %s", exc)
            continue
        for row in resp.get("Responses", {}).get(table_name, []):
            url = row.get("article_url")
            rating = row.get("rating")
            if url and rating:
                ratings[url] = rating
    return ratings


def _query_feedback_bucket(bucket: str, limit: int) -> list[dict[str, Any]]:
    resp = _t_feedback().query(
        IndexName=_FEEDBACK_GSI,
        KeyConditionExpression=Key("bucket").eq(bucket),
        ScanIndexForward=False,
        Limit=limit,
    )
    return resp.get("Items", [])


def recent_feedback(limit: int = 50, *, now: datetime | None = None) -> list[FeedbackRow]:
    """Return the ``limit`` most-recent feedback rows, newest first.

    Reads from the current-year shard first; if it doesn't have enough
    rows (e.g., early January), supplements from the previous year.
    """
    now = now or datetime.now(UTC)
    items: list[dict[str, Any]] = _query_feedback_bucket(_feedback_bucket(now), limit)
    if len(items) < limit:
        prev = now.replace(year=now.year - 1)
        items.extend(_query_feedback_bucket(_feedback_bucket(prev), limit - len(items)))

    rows: list[FeedbackRow] = []
    for item in items:
        try:
            rows.append(
                FeedbackRow(
                    article_url=item["article_url"],
                    title=item.get("title", ""),
                    rating=item["rating"],
                    ts=datetime.fromisoformat(item["ts"]),
                    issue_date=item.get("issue_date", ""),
                    note=item.get("note", ""),
                )
            )
        except (ValidationError, KeyError, ValueError) as exc:
            log.warning("skipping bad feedback row: %s", exc)
    rows.sort(key=lambda r: r.ts, reverse=True)
    return rows[:limit]


def issue_exists(date: str) -> bool:
    """True if an Issue row has been written for ``date`` (regardless of send status)."""
    resp = _t_issues().get_item(
        Key={"date": date},
        ProjectionExpression="#d",
        ExpressionAttributeNames={"#d": "date"},
    )
    return "Item" in resp


def issue_sent(date: str) -> bool:
    """True if ``date``'s issue has been successfully emailed.

    This is the real idempotency marker — distinct from ``issue_exists``
    so a partial failure (Issue stored but email send failed) doesn't
    silently skip the retry.
    """
    resp = _t_issues().get_item(
        Key={"date": date},
        ProjectionExpression="sent_at",
    )
    item = resp.get("Item") or {}
    return bool(item.get("sent_at"))


def mark_issue_sent(date: str) -> None:
    """Flip the ``sent_at`` flag on today's issue. Called only after a
    successful email send."""
    _t_issues().update_item(
        Key={"date": date},
        UpdateExpression="SET sent_at = :ts",
        ExpressionAttributeValues={":ts": datetime.now(UTC).isoformat()},
    )


def list_issues(limit: int = 30) -> list[dict[str, Any]]:
    """Return a recent set of issues for the admin index, newest first.

    Returns lightweight dicts (date + pick count + sent status); avoids
    pulling the full picks JSON for each row.
    """
    resp = _t_issues().scan(
        ProjectionExpression="#d, sent_at, picks_json, #m",
        ExpressionAttributeNames={"#d": "date", "#m": "manual"},
    )
    rows = []
    for item in resp.get("Items", []):
        # Manual "send now" issues are kept out of "recent issues".
        if item.get("manual"):
            continue
        try:
            picks_count = len(json.loads(item.get("picks_json", "[]")))
        except json.JSONDecodeError:
            picks_count = 0
        rows.append(
            {
                "date": item["date"],
                "picks_count": picks_count,
                "sent_at": item.get("sent_at"),
            }
        )
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows[:limit]


# ---------------------------------------------------------------------------
# Newsletter subscriptions (inbound-email data source)
# ---------------------------------------------------------------------------


def _subscription_from_item(item: dict[str, Any]) -> Subscription:
    """Build a Subscription from a raw row; lenient on optional fields."""
    return Subscription(
        address=item["address"],
        source=item.get("source", ""),
        status=item.get("status", "pending"),
        created_at=datetime.fromisoformat(item["created_at"]),
        confirmed_at=(
            datetime.fromisoformat(item["confirmed_at"])
            if item.get("confirmed_at")
            else None
        ),
        last_received_at=(
            datetime.fromisoformat(item["last_received_at"])
            if item.get("last_received_at")
            else None
        ),
    )


def add_subscription(source: str, *, address: str) -> Subscription:
    """Create a pending subscription bound to a freshly generated address.

    ``address`` is stored lowercased so the inbound router (which lowercases
    SES recipients) always matches regardless of how the sender cased it.
    """
    addr = address.strip().lower()
    now = datetime.now(UTC)
    _t_subscriptions().put_item(
        Item={
            "address": addr,
            "source": source,
            "status": "pending",
            "created_at": now.isoformat(),
        }
    )
    return Subscription(address=addr, source=source, status="pending", created_at=now)


def list_subscriptions() -> list[Subscription]:
    """All subscriptions, newest first; skips-and-logs bad/legacy rows."""
    items = _t_subscriptions().scan().get("Items", [])
    subs: list[Subscription] = []
    for item in items:
        try:
            subs.append(_subscription_from_item(item))
        except (ValidationError, KeyError, ValueError) as exc:
            log.warning("skipping bad subscription row %r: %s", item.get("address"), exc)
    subs.sort(key=lambda s: s.created_at, reverse=True)
    return subs


def get_subscription(address: str) -> Subscription | None:
    """Look up one subscription by address (case-insensitive)."""
    resp = _t_subscriptions().get_item(Key={"address": address.strip().lower()})
    item = resp.get("Item")
    if not item:
        return None
    try:
        return _subscription_from_item(item)
    except (ValidationError, KeyError, ValueError) as exc:
        log.warning("bad subscription row for %r: %s", address, exc)
        return None


def delete_subscription(address: str) -> None:
    _t_subscriptions().delete_item(Key={"address": address.strip().lower()})


def mark_subscription_confirmed(address: str, *, when: datetime | None = None) -> None:
    """Flip a subscription to ``confirmed`` after a successful opt-in follow.

    ``status`` is a DynamoDB reserved word, so it is aliased via
    ExpressionAttributeNames.
    """
    when = when or datetime.now(UTC)
    _t_subscriptions().update_item(
        Key={"address": address.strip().lower()},
        UpdateExpression="SET #s = :s, confirmed_at = :c",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "confirmed", ":c": when.isoformat()},
    )


def touch_subscription(address: str, *, when: datetime | None = None) -> None:
    """Record that mail was just received for this subscription."""
    when = when or datetime.now(UTC)
    _t_subscriptions().update_item(
        Key={"address": address.strip().lower()},
        UpdateExpression="SET last_received_at = :t",
        ExpressionAttributeValues={":t": when.isoformat()},
    )


# ---------------------------------------------------------------------------
# Inbox (received newsletter emails -> extracted article candidates)
# ---------------------------------------------------------------------------


def put_inbox_email(
    *,
    message_id: str,
    source: str,
    address: str,
    articles: list[Article],
    received_at: datetime,
) -> None:
    """Persist one received newsletter's extracted article candidates.

    Stored as a single row per email (articles as JSON) with a TTL, plus a
    per-year ``bucket`` so :func:`recent_inbox_articles` can read a time range
    off the GSI without scanning. A blank ``message_id`` (shouldn't happen from
    SES) gets a synthetic key so the put never silently no-ops.
    """
    import uuid

    mid = message_id or ("inbox-" + uuid.uuid4().hex)
    articles_json = json.dumps([json.loads(a.model_dump_json()) for a in articles])
    expires_at = int(received_at.timestamp() + _INBOX_TTL_SECONDS)
    _t_inbox().put_item(
        Item={
            "message_id": mid,
            "received_at": received_at.isoformat(),
            "source": source,
            "address": address,
            "articles_json": articles_json,
            "bucket": _inbox_bucket(received_at),
            "expires_at": expires_at,
        }
    )


def _query_inbox_bucket(bucket: str, since: datetime) -> list[dict[str, Any]]:
    resp = _t_inbox().query(
        IndexName=_INBOX_GSI,
        KeyConditionExpression=Key("bucket").eq(bucket)
        & Key("received_at").gte(since.isoformat()),
    )
    return resp.get("Items", [])


def recent_inbox_articles(since: datetime, *, now: datetime | None = None) -> list[Article]:
    """Article candidates from newsletters received since ``since``.

    Reads the current-year shard and, when ``since`` falls in the prior year
    (the early-January edge), the previous one too — mirroring
    :func:`recent_feedback`. Lenient: a bad row or article is skipped, never
    fatal, since the digest must not break on one malformed inbound email.
    """
    now = now or datetime.now(UTC)
    items = _query_inbox_bucket(_inbox_bucket(now), since)
    if _inbox_bucket(since) != _inbox_bucket(now):
        items += _query_inbox_bucket(_inbox_bucket(since), since)

    articles: list[Article] = []
    seen: set[str] = set()
    for item in items:
        try:
            raw = json.loads(item.get("articles_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            log.warning("skipping inbox row with bad articles_json: %r", item.get("message_id"))
            continue
        for entry in raw:
            try:
                art = Article.model_validate(entry)
            except ValidationError:
                continue
            key = str(art.url)
            if key in seen:
                continue
            seen.add(key)
            articles.append(art)
    return articles
