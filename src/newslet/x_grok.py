"""X (Twitter) as a ranking source, via xAI's Grok with Live Search.

X's own API has no free read tier — pulling timelines/search needs the paid
Basic plan ($200/mo+). xAI's Grok API instead exposes **Live Search**, which
can read X directly as a search source and bills per-use (cents for a daily
digest's handful of posts), so we get fresh, on-profile posts without the flat
monthly floor and without scraping.

Shape mirrors the other ranking-pool sources (:mod:`newslet.hn`,
:mod:`newslet.newsletters`): :func:`fetch_x_articles` returns ``list[Article]``
that joins the digest's candidate pool, so X posts compete with RSS/HN for the
day's picks. Like those sources it carries no admin knob — it is simply on
whenever an ``XAI_API_KEY`` is configured and degrades to an empty list
otherwise.

Best-effort throughout: a missing key, a network/parse error, or an empty reply
yields ``[]`` rather than raising — X must never block a send. The network edge
is an injected ``complete`` callable (a single chat-completion request → parsed
JSON response) so tests stay offline and we pull in no new SDK dependency.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.request import Request, urlopen

from pydantic import ValidationError

from .config import settings
from .contracts import Article
from .discovery import _extract_json_object  # shared JSON-from-model-reply helper

logger = logging.getLogger(__name__)

# xAI's OpenAI-compatible chat-completions endpoint. Live Search is requested
# via the ``search_parameters`` extension on the request body.
_XAI_ENDPOINT = "https://api.x.ai/v1/chat/completions"
_REQUEST_TIMEOUT = 20
_USER_AGENT = "newslet/1.0 (+https://github.com/jfrederick/newslet)"

# How recent a post must be to count as "today's" — mirrors the digest's 24h
# RSS window, with a day of slack so timezone boundaries don't drop anything.
_RECENCY = timedelta(days=2)

_SYSTEM_PROMPT = """\
You surface the best recent posts from X (Twitter) for a personalized daily \
news digest. Use Live Search over X to find up to {max_results} high-signal, \
RECENT posts that match the user's interests. Prefer substantive posts from \
credible accounts — analysis, primary-source news, expert threads — over memes, \
engagement bait, and ads. Skip near-duplicates.

Reply with ONLY a JSON object (no prose, no markdown fences) matching this \
schema:

{{
  "posts": [
    {{
      "url":     "<canonical link to the post (https://x.com/<user>/status/<id>)>",
      "author":  "<the posting account's @handle>",
      "text":    "<the post's text, lightly trimmed>",
      "likes":   <integer like count, or 0 if unknown>,
      "reposts": <integer repost count, or 0 if unknown>
    }}
  ]
}}
"""


def _default_complete(payload: dict, api_key: str) -> dict:
    """POST ``payload`` to the xAI endpoint and return the parsed JSON reply.

    Used in production; tests inject a fake ``complete`` instead.
    """
    body = json.dumps(payload).encode("utf-8")
    req = Request(  # noqa: S310 - https only, constant endpoint
        _XAI_ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        },
    )
    with urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:  # noqa: S310 - https only
        return json.loads(resp.read().decode("utf-8"))


def _message_content(response: dict) -> str | None:
    """Pull the assistant message text out of an OpenAI-shaped reply."""
    if not isinstance(response, dict):
        return None
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, str) else None


def _summary_for(post: dict) -> str:
    """A ranking-useful one-liner: engagement signal plus the post text."""
    author = str(post.get("author") or "?").lstrip("@")
    likes = post.get("likes") or 0
    reposts = post.get("reposts") or 0
    head = f"{likes} likes, {reposts} reposts on X (by @{author})."
    text = " ".join(str(post.get("text") or "").split())
    if text:
        head += " " + (text[:240] + "…" if len(text) > 240 else text)
    return head


def _title_for(post: dict) -> str:
    """Posts have no title; derive a readable one from the text or handle."""
    text = " ".join(str(post.get("text") or "").split())
    if text:
        return text[:100] + "…" if len(text) > 100 else text
    author = str(post.get("author") or "").lstrip("@")
    return f"@{author} on X" if author else "Post on X"


def _post_to_article(post: dict, *, now: datetime) -> Article | None:
    """Build a ranking :class:`Article` from one Grok post object.

    Returns ``None`` (and logs) if the post lacks a usable url. ``published``
    defaults to ``now`` — Grok rarely returns a reliable timestamp, and the
    ranker uses the engagement-rich ``summary``, not the date.
    """
    url = post.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        return Article(
            url=url.strip(),
            title=_title_for(post),
            summary=_summary_for(post),
            source="X",
            published=now,
        )
    except ValidationError as exc:
        logger.info("x: skipping unrankable post %s: %s", url, exc)
        return None


def fetch_x_articles(
    query: str,
    *,
    max_results: int = 15,
    recent: bool = True,
    api_key: str | None = None,
    model: str | None = None,
    complete: Callable[[dict, str], dict] | None = None,
    now: datetime | None = None,
) -> list[Article]:
    """Return recent on-profile X posts as ranking candidates, best-effort.

    ``query`` is a profile-distilled request (the digest reuses the same
    distillation as the web-search block). ``api_key``/``model`` default to the
    configured xAI credentials; when no key is configured this returns ``[]``
    without a network call, so the source is simply disabled until a key exists.
    Any failure (bad JSON, empty reply, network error) also returns ``[]`` — X
    must never break the digest.
    """
    if not query.strip():
        return []
    api_key = api_key if api_key is not None else settings().xai_api_key
    if not api_key:
        logger.info("x: no XAI_API_KEY configured; skipping X source")
        return []

    complete = complete or _default_complete
    now = now or datetime.now(UTC)
    model = model or settings().xai_model

    search_parameters: dict = {
        "mode": "on",
        "sources": [{"type": "x"}],
        "max_search_results": max(1, max_results),
        "return_citations": True,
    }
    if recent:
        search_parameters["from_date"] = (now - _RECENCY).strftime("%Y-%m-%d")

    payload = {
        "model": model,
        "temperature": 0,
        "search_parameters": search_parameters,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT.format(max_results=max_results)},
            {"role": "user", "content": query.strip()},
        ],
    }

    try:
        response = complete(payload, api_key)
    except Exception as exc:  # noqa: BLE001 - best effort; never raise
        logger.warning("x: API call failed: %s", exc)
        return []

    text = _message_content(response)
    if text is None:
        logger.warning("x: no message content in response")
        return []

    json_str = _extract_json_object(text)
    if json_str is None:
        logger.warning("x: no JSON object found in response: %.200s", text)
        return []

    try:
        payload_out = json.loads(json_str)
    except json.JSONDecodeError as err:
        logger.warning("x: could not parse response: %s", err)
        return []

    raw = payload_out.get("posts", []) if isinstance(payload_out, dict) else []

    out: list[Article] = []
    seen_urls: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        article = _post_to_article(item, now=now)
        if article is None:
            continue
        key = str(article.url)
        if key in seen_urls:
            continue
        seen_urls.add(key)
        out.append(article)
        if len(out) >= max_results:
            break
    return out
