"""Serendipity: "off your beat" articles via Claude's server-side web search.

A best-effort source that surfaces popular, high-quality pieces from the
past week the reader might enjoy for reasons that have nothing to do with
their day job. It uses the profile only to infer the reader's *broader
human* tastes (culture, sport, food, science, travel, human interest, ...)
and deliberately excludes anything about computers, software, programming,
AI/ML, or the tech industry — the point of this source is to give the
reader a break from their usual beat, not another angle on it.

Mirrors :mod:`newslet.websearch` closely: same server-side web_search tool,
same JSON-object reply contract, same tolerant parsing via
:mod:`newslet.search_common`. Best-effort throughout — any failure (API
error, unparsable reply, malformed item) yields an empty/partial list
rather than raising, since this is a nice-to-have, never a blocker for the
rest of the digest.
"""

from __future__ import annotations

import json
import logging

import anthropic
from pydantic import TypeAdapter, ValidationError

from .config import settings
from .contracts import WebArticle
from .search_common import extract_json_object, last_text_block, web_search_tool

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a discerning generalist editor. Use the web_search tool to find up \
to {max_results} popular, widely-shared, high-quality articles published \
within roughly the PAST WEEK that a person with the reader's broader human \
interests and hobbies would enjoy reading purely for pleasure.

Use the reader's profile ONLY to infer their broader human tastes — the \
kind of person they are outside of work — not their professional or \
technical interests.

HARD EXCLUSION, this is the entire point of this feature: do NOT include \
anything about computers, software, programming, artificial intelligence, \
machine learning, or the tech industry, even if it is tangentially related \
to something in the reader's profile. If an article touches computing or \
AI in any way, leave it out. The reader gets plenty of that elsewhere; this \
list exists to give them a break from it.

Serious journalism, science (non-computing), culture, sport, food, travel, \
and human-interest stories are all fair game. Prefer pieces that are \
genuinely popular and widely shared right now, from reputable publications; \
avoid SEO spam, listicles, and duplicates.

After searching, reply with ONLY a JSON object (no prose, no markdown \
fences) matching this schema:

{{
  "articles": [
    {{
      "url":    "<article url>",
      "title":  "<article title>",
      "source": "<publication name>",
      "blurb":  "<one sentence on what the article covers; describe the topic \
only, not why it is worth reading>"
    }}
  ]
}}
"""

_articles_adapter = TypeAdapter(list[WebArticle])

_GENERIC_PROFILE = "A curious generalist reader with no stated interests on file."


def _build_user_block(profile_md: str) -> str:
    profile = profile_md.strip() or _GENERIC_PROFILE
    return f"# Reader profile\n{profile}"


def fetch_serendipity(
    profile_md: str,
    *,
    max_results: int = 4,
    client: anthropic.Anthropic | None = None,
    max_searches: int = 2,
    model: str | None = None,
) -> list[WebArticle]:
    """Fetch up to ``max_results`` "off your beat" articles for the reader.

    ``profile_md`` is the reader's free-text profile; an empty profile falls
    back to a generic "curious generalist reader" line so the search still
    has something to work with. ``max_searches`` caps the tool rounds and
    ``model`` overrides the configured model, same as :mod:`websearch`.
    Returns ``[]`` on any failure — a parse error, an empty reply, or an API
    exception — since this source is best-effort and must never break the
    rest of the digest.
    """
    if max_results <= 0:
        return []
    if client is None:
        client = anthropic.Anthropic(api_key=settings().anthropic_api_key)

    try:
        response = client.messages.create(
            model=model or settings().claude_model,
            max_tokens=2048,
            system=_SYSTEM_PROMPT.format(max_results=max_results),
            tools=[web_search_tool(max_searches)],
            messages=[
                {
                    "role": "user",
                    "content": _build_user_block(profile_md),
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001 - best effort; never raise
        logger.warning("serendipity: API call failed: %s", exc)
        return []

    text = last_text_block(response.content)
    if text is None:
        logger.warning("serendipity: no text block in response")
        return []

    json_str = extract_json_object(text)
    if json_str is None:
        logger.warning("serendipity: no JSON object found: %.200s", text)
        return []

    try:
        payload = json.loads(json_str)
    except json.JSONDecodeError as err:
        logger.warning("serendipity: could not parse response: %s", err)
        return []

    raw = payload.get("articles", []) if isinstance(payload, dict) else []

    out: list[WebArticle] = []
    seen_urls: set[str] = set()
    for item in raw:
        try:
            article = _articles_adapter.validate_python([item])[0]
        except ValidationError as err:
            logger.info("serendipity: dropping malformed result: %s", err)
            continue
        key = str(article.url)
        if key in seen_urls:
            continue
        seen_urls.add(key)
        out.append(article)
        if len(out) >= max_results:
            break
    return out
