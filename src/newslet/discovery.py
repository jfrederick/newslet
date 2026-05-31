"""Discover new sources via Claude's server-side web search.

:func:`find_discoveries` asks Claude to run a live web search for recent
articles that fit the user's profile but come from domains the user does
*not* already follow.  Discovery is best-effort: any failure (bad JSON,
empty response) yields an empty list rather than crashing the digest.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from urllib.parse import urlsplit

import anthropic
import feedparser
from pydantic import TypeAdapter, ValidationError

from .config import settings
from .contracts import Discovery

logger = logging.getLogger(__name__)

# Server-side web search tool. Pin to the version string the API expects;
# tests never call the real API so the exact value is not load-bearing here.
_WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 5,
}

_SYSTEM_PROMPT = """\
You discover fresh news sources for a personalized daily email digest.

Use the web_search tool to find up to {max_results} RECENT articles from
reputable sources that match the user's profile.  Only surface articles
from sources the user does NOT already follow: EXCLUDE any url whose
registered domain appears in the user's existing feed domains.

CRITICAL: only include an article if its source publishes a public
RSS/Atom feed, and put that feed's URL in "feed_url".  The digest
subscribes to that feed to pull future articles, so it must be a real
feed endpoint (e.g. https://example.com/feed, .../rss, .../atom.xml) —
NOT the article URL, the homepage, or a social profile.  If you cannot
find a working feed for a source, DROP that article rather than guessing.

After searching, reply with ONLY a JSON object (no prose, no markdown
fences) matching this schema:

{{
  "discoveries": [
    {{
      "url":      "<article url>",
      "title":    "<article title>",
      "source":   "<publication name>",
      "reason":   "<one short sentence on why it fits the profile>",
      "feed_url": "<the source's RSS/Atom feed url>"
    }}
  ]
}}
"""

_discovery_adapter = TypeAdapter(Discovery)


def _host_key(url: str) -> str:
    """Return the lowercased host of ``url`` without a leading ``www.``.

    This is a host-level backstop, not a true registered-domain (eTLD+1)
    extractor: ``news.bbc.co.uk`` and ``bbc.co.uk`` produce different keys.
    The primary "exclude sources the user already follows" rule is enforced
    by the model via the system prompt; this filter just catches exact-host
    repeats without pulling in a public-suffix dependency.
    """
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _last_text_block(content: list) -> str | None:
    """Return the text of the final text block in ``content``.

    With the web search tool the response interleaves several content
    blocks (tool calls, results), so the model's final JSON is in the
    *last* text block, not ``content[0]``.
    """
    for block in reversed(content):
        if getattr(block, "type", None) == "text" or hasattr(block, "text"):
            text = getattr(block, "text", None)
            if text is not None:
                return text
    return None


def _extract_json_object(text: str) -> str | None:
    """Pull the discoveries JSON object out of a model reply.

    With the web_search tool active the model often ignores the
    "ONLY a JSON object, no fences" instruction and wraps its answer in a
    ```json fence, prefixes a sentence ("Here are the articles..."), or
    trails one ("...}\nHope that helps!"). A bare ``json.loads`` on any of
    those raises and — because discovery is best-effort — the section
    silently vanishes from the email. Prefer the first fenced block that
    looks like an object, then return the first balanced ``{...}`` span,
    ignoring braces inside string literals so surrounding prose (or a stray
    brace in a title/reason) can't kill an otherwise-valid payload.
    """
    candidate = text.strip()

    # If the answer is fenced, take the first fenced block that actually
    # looks like a JSON object — the model sometimes emits an unrelated
    # example fence before the real one.
    for body in re.findall(r"```(?:json)?\s*(.*?)\s*```", candidate, re.DOTALL):
        body = body.strip()
        if body.startswith("{"):
            candidate = body
            break

    # Return the first balanced {...} span. A string-literal toggle keeps
    # braces inside values (e.g. "covers {tech} topics") from skewing the
    # depth count, and stopping at depth 0 trims any trailing prose.
    start = candidate.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(candidate)):
        ch = candidate[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return candidate[start : i + 1]
    return None


def _feed_is_live(feed_url: str) -> bool:
    """Return True if ``feed_url`` actually parses as an RSS/Atom feed.

    The model asserts a ``feed_url`` but can hallucinate a plausible-looking
    one. We fetch it and require feedparser to find at least one entry and
    no fatal (``bozo``) parse error — the same liveness bar ``feeds.py``
    applies on the real fetch — so we never offer a "subscribe" button for
    a dead URL. Best-effort: any network/parse failure means "not live",
    never an exception out of discovery.
    """
    try:
        parsed = feedparser.parse(feed_url)
    except Exception as exc:  # noqa: BLE001 - feedparser can raise anything
        logger.info("discovery: feed %s failed to fetch: %s", feed_url, exc)
        return False

    if getattr(parsed, "bozo", 0) and getattr(parsed, "bozo_exception", None):
        logger.info(
            "discovery: feed %s is malformed: %s", feed_url, parsed.bozo_exception
        )
        return False

    if not getattr(parsed, "entries", None):
        logger.info("discovery: feed %s has no entries", feed_url)
        return False

    return True


def find_discoveries(
    profile_md: str,
    feed_domains: list[str],
    *,
    client: anthropic.Anthropic | None = None,
    max_results: int = 2,
    feed_validator: Callable[[str], bool] | None = None,
) -> list[Discovery]:
    """Find up to ``max_results`` articles outside the user's feeds.

    Each surviving discovery's ``feed_url`` is fetched and confirmed to be a
    real, non-empty RSS/Atom feed before it is offered with a subscribe
    link; the check is injectable via ``feed_validator`` so tests don't hit
    the network. Returns an empty list on any parse failure; discovery is
    best-effort and must never crash the digest pipeline.
    """
    if feed_validator is None:
        feed_validator = _feed_is_live
    if client is None:
        client = anthropic.Anthropic(api_key=settings().anthropic_api_key)

    excluded = {_host_key(f"//{d}") or d.lower() for d in feed_domains}

    system_prompt = _system_prompt(max_results)
    user_block = _build_user_block(profile_md, feed_domains)

    response = client.messages.create(
        model=settings().claude_model,
        max_tokens=2048,
        system=system_prompt,
        tools=[_WEB_SEARCH_TOOL],
        messages=[{"role": "user", "content": user_block}],
    )

    text = _last_text_block(response.content)
    if text is None:
        logger.warning("discovery: no text block in response")
        return []

    json_str = _extract_json_object(text)
    if json_str is None:
        logger.warning("discovery: no JSON object found in response: %.200s", text)
        return []

    try:
        payload = json.loads(json_str)
    except json.JSONDecodeError as err:
        logger.warning("discovery: could not parse response: %s", err)
        return []
    raw = payload.get("discoveries", []) if isinstance(payload, dict) else []

    # Validate item-by-item so one bad entry — e.g. a missing/invalid
    # feed_url, which is now required — drops just that article instead of
    # discarding the whole batch.
    discoveries: list[Discovery] = []
    for item in raw:
        try:
            discoveries.append(_discovery_adapter.validate_python(item))
        except ValidationError as err:
            logger.info("discovery: dropping item without a usable feed: %s", err)

    # Exclude already-followed domains, then keep only those whose feed_url
    # actually resolves to a live feed. Validate lazily and stop at
    # max_results so we don't fetch feeds we'd only discard.
    kept: list[Discovery] = []
    for d in discoveries:
        if _host_key(str(d.url)) in excluded:
            continue
        if not feed_validator(str(d.feed_url)):
            logger.info("discovery: dropping %s — feed not live", d.url)
            continue
        kept.append(d)
        if len(kept) >= max_results:
            break
    return kept


def _system_prompt(max_results: int) -> str:
    """Render the system prompt with the target result count."""
    return _SYSTEM_PROMPT.format(max_results=max_results)


def _build_user_block(profile_md: str, feed_domains: list[str]) -> str:
    """Format the profile + already-followed domains into the user turn."""
    domains = ", ".join(feed_domains) if feed_domains else "(none)"
    return (
        "# User profile\n"
        f"{profile_md.strip()}\n\n"
        "# Feed domains the user already follows (exclude these)\n"
        f"{domains}"
    )
