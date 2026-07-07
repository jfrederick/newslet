"""Build the Discover page's board of recommended sources.

Distinct from :mod:`newslet.discovery`: that module surfaces individual
*articles* outside the user's feeds for the daily email, refreshed on every
digest run. This module operates one level up — it recommends *sources*
(RSS/Atom feeds and X accounts) that match the user's profile, and builds a
:class:`~newslet.contracts.DiscoverBoard` that is stored and shown on a
dedicated Discover page, regenerated on its own schedule rather than per
digest.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime

import anthropic
from pydantic import TypeAdapter, ValidationError

from .config import settings
from .contracts import DiscoverAccount, DiscoverBoard, DiscoverFeed
from .search_common import (
    extract_json_object,
    feed_is_live,
    host_key,
    last_text_block,
    web_search_tool,
)

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You recommend new sources for a personalized "Discover" page.

Use the web_search tool to find:

1. Up to {max_feeds} publications or blogs that match the user's profile \
and publish a public RSS/Atom feed. For each, give its title, site_url, \
feed_url, and a one-line reason it fits the profile. CRITICAL: only \
include a publication if it has a real, working RSS/Atom feed endpoint \
(e.g. https://example.com/feed, .../rss, .../atom.xml) — NOT the homepage \
or an article URL. If you cannot find a working feed, DROP that \
publication rather than guessing. EXCLUDE any publication whose domain the \
user already follows (see the domains listed below).

2. Up to {max_accounts} active, high-signal X (Twitter) accounts that match \
the user's profile. For each, give its handle (without the @), display \
name, and a one-line reason it fits the profile. Prefer accounts that post \
often and are still active; avoid dormant or low-signal accounts.

After searching, reply with ONLY a JSON object (no prose, no markdown
fences) matching this schema:

{{
  "feeds": [
    {{
      "title":    "<publication name>",
      "site_url": "<homepage url>",
      "feed_url": "<RSS/Atom feed url>",
      "reason":   "<one short sentence on why it fits the profile>"
    }}
  ],
  "accounts": [
    {{
      "handle": "<x/twitter handle, without @>",
      "name":   "<display name>",
      "reason": "<one short sentence on why it fits the profile>"
    }}
  ]
}}
"""

_feed_adapter = TypeAdapter(DiscoverFeed)
_account_adapter = TypeAdapter(DiscoverAccount)

_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


def build_discover_board(
    profile_md: str,
    followed_domains: list[str],
    *,
    client: anthropic.Anthropic | None = None,
    max_feeds: int = 6,
    max_accounts: int = 6,
    feed_validator: Callable[[str], bool] | None = None,
    now: datetime | None = None,
) -> DiscoverBoard:
    """Build a :class:`DiscoverBoard` of recommended feeds and X accounts.

    One Claude call asks for both a feed list and an account list matching
    ``profile_md``, excluding domains in ``followed_domains``. Each
    surviving feed's ``feed_url`` is confirmed live before it is offered
    (injectable via ``feed_validator`` so tests don't hit the network,
    defaulting to :func:`newslet.search_common.feed_is_live`). Best-effort:
    any failure (bad JSON, empty response, API error) yields an empty
    ``DiscoverBoard`` with ``generated_at=None`` rather than raising;
    ``generated_at`` is stamped with ``now`` only on a successful parse.
    """
    if feed_validator is None:
        feed_validator = feed_is_live
    if client is None:
        client = anthropic.Anthropic(api_key=settings().anthropic_api_key)
    if now is None:
        now = datetime.now(UTC)

    excluded = {host_key(f"//{d}") or d.lower() for d in followed_domains}

    system_prompt = _SYSTEM_PROMPT.format(
        max_feeds=max_feeds, max_accounts=max_accounts
    )
    user_block = _build_user_block(profile_md, followed_domains)

    try:
        response = client.messages.create(
            model=settings().claude_model,
            max_tokens=4096,
            system=system_prompt,
            tools=[web_search_tool()],
            messages=[{"role": "user", "content": user_block}],
        )
    except Exception as exc:  # noqa: BLE001 - best effort; never raise
        logger.warning("discover: API call failed: %s", exc)
        return DiscoverBoard()

    text = last_text_block(response.content)
    if text is None:
        logger.warning("discover: no text block in response")
        return DiscoverBoard()

    json_str = extract_json_object(text)
    if json_str is None:
        logger.warning("discover: no JSON object found in response: %.200s", text)
        return DiscoverBoard()

    try:
        payload = json.loads(json_str)
    except json.JSONDecodeError as err:
        logger.warning("discover: could not parse response: %s", err)
        return DiscoverBoard()

    if not isinstance(payload, dict):
        logger.warning("discover: top-level JSON was not an object")
        return DiscoverBoard()

    feeds = _build_feeds(
        payload.get("feeds", []), excluded, feed_validator, max_feeds
    )
    accounts = _build_accounts(payload.get("accounts", []), max_accounts)

    return DiscoverBoard(feeds=feeds, accounts=accounts, generated_at=now)


def _build_feeds(
    raw: list,
    excluded: set[str],
    feed_validator: Callable[[str], bool],
    max_feeds: int,
) -> list[DiscoverFeed]:
    """Validate, exclude followed domains, and lazily liveness-check feeds."""
    # Validate item-by-item so one bad entry doesn't discard the whole batch.
    candidates: list[DiscoverFeed] = []
    for item in raw:
        try:
            candidates.append(_feed_adapter.validate_python(item))
        except ValidationError as err:
            logger.info("discover: dropping malformed feed: %s", err)

    # Exclude already-followed domains, then keep only those whose feed_url
    # actually resolves to a live feed. Validate lazily and stop at
    # max_feeds so we don't fetch feeds we'd only discard.
    kept: list[DiscoverFeed] = []
    for f in candidates:
        if host_key(str(f.site_url)) in excluded or host_key(str(f.feed_url)) in excluded:
            continue
        if not feed_validator(str(f.feed_url)):
            logger.info("discover: dropping %s — feed not live", f.site_url)
            continue
        kept.append(f)
        if len(kept) >= max_feeds:
            break
    return kept


def _build_accounts(raw: list, max_accounts: int) -> list[DiscoverAccount]:
    """Normalize handles, drop invalid ones, dedupe, and cap the result."""
    kept: list[DiscoverAccount] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        handle = str(item.get("handle", "")).strip().lstrip("@")
        if not _HANDLE_RE.match(handle):
            logger.info("discover: dropping invalid handle: %r", handle)
            continue
        key = handle.lower()
        if key in seen:
            continue
        try:
            account = _account_adapter.validate_python(
                {
                    "handle": handle,
                    "name": item.get("name", ""),
                    "reason": item.get("reason", ""),
                    "url": f"https://x.com/{handle}",
                }
            )
        except ValidationError as err:
            logger.info("discover: dropping malformed account: %s", err)
            continue
        seen.add(key)
        kept.append(account)
        if len(kept) >= max_accounts:
            break
    return kept


def _build_user_block(profile_md: str, followed_domains: list[str]) -> str:
    """Format the profile + already-followed domains into the user turn."""
    domains = ", ".join(followed_domains) if followed_domains else "(none)"
    return (
        "# User profile\n"
        f"{profile_md.strip()}\n\n"
        "# Feed domains the user already follows (exclude these)\n"
        f"{domains}"
    )
