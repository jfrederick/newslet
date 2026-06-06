"""On-demand web search via Claude's server-side ``web_search`` tool.

Two consumers:

- The digest calls :func:`search_web` with a query distilled from the user's
  profile to fill the web view's "from around the web" block (the 20 articles
  pulled from the open web, separate from the RSS/HN picks).
- The web view's subject box ("type a subject, the search hones in") calls it
  live with whatever the user typed.

Best-effort throughout: a parse failure or empty model reply yields an empty
list rather than raising. The Anthropic client is injectable so tests never
touch the network. JSON extraction reuses :mod:`newslet.discovery`'s helpers,
which already tolerate the fences/prose the model emits when the search tool
is active.
"""

from __future__ import annotations

import json
import logging

import anthropic
from pydantic import TypeAdapter, ValidationError

from .config import settings
from .contracts import WebArticle
from .discovery import _extract_json_object, _host_key, _last_text_block

logger = logging.getLogger(__name__)

def _web_search_tool(max_searches: int) -> dict:
    """The server-side web_search tool, capped at ``max_searches`` rounds.

    The interactive subject box runs behind an HTTP API with a hard ~30s
    integration timeout, so it passes a low cap (and a fast model) to stay
    well under it; the digest, with a 300s Lambda budget, can search more.
    """
    return {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": max(1, max_searches),
    }

_SYSTEM_PROMPT = """\
You are a research librarian. Use the web_search tool to find up to \
{max_results} high-quality, RECENT articles that best match the user's \
request. Prefer primary sources and reputable publications; avoid SEO spam, \
listicles, and duplicates.

After searching, reply with ONLY a JSON object (no prose, no markdown \
fences) matching this schema:

{{
  "articles": [
    {{
      "url":    "<article url>",
      "title":  "<article title>",
      "source": "<publication name>",
      "blurb":  "<one sentence on what it covers and why it is worth reading>"
    }}
  ]
}}
"""

_articles_adapter = TypeAdapter(list[WebArticle])


def _build_user_block(query: str, *, recent: bool) -> str:
    recency = (
        "Focus on material published in roughly the last week.\n\n"
        if recent
        else ""
    )
    return f"{recency}# Request\n{query.strip()}"


def search_web(
    query: str,
    *,
    max_results: int = 20,
    recent: bool = True,
    client: anthropic.Anthropic | None = None,
    exclude_hosts: list[str] | None = None,
    max_searches: int = 3,
    model: str | None = None,
) -> list[WebArticle]:
    """Search the open web for ``query`` and return up to ``max_results``.

    ``exclude_hosts`` drops results from hosts already covered elsewhere
    (e.g. the user's own feeds), de-duplicated by registered-ish host the
    same way :mod:`discovery` does. ``max_searches`` caps the tool rounds and
    ``model`` overrides the configured model — the interactive subject box
    passes a low cap and a fast model so it returns within the HTTP API's
    ~30s limit. Returns ``[]`` on any failure.
    """
    if not query.strip():
        return []
    if client is None:
        client = anthropic.Anthropic(api_key=settings().anthropic_api_key)

    excluded = {_host_key(f"//{h}") or h.lower() for h in (exclude_hosts or [])}

    try:
        response = client.messages.create(
            model=model or settings().claude_model,
            max_tokens=2048,
            system=_SYSTEM_PROMPT.format(max_results=max_results),
            tools=[_web_search_tool(max_searches)],
            messages=[
                {"role": "user", "content": _build_user_block(query, recent=recent)}
            ],
        )
    except Exception as exc:  # noqa: BLE001 - best effort; never raise
        logger.warning("websearch: API call failed: %s", exc)
        return []

    text = _last_text_block(response.content)
    if text is None:
        logger.warning("websearch: no text block in response")
        return []

    json_str = _extract_json_object(text)
    if json_str is None:
        logger.warning("websearch: no JSON object found: %.200s", text)
        return []

    try:
        payload = json.loads(json_str)
    except json.JSONDecodeError as err:
        logger.warning("websearch: could not parse response: %s", err)
        return []

    raw = payload.get("articles", []) if isinstance(payload, dict) else []

    out: list[WebArticle] = []
    seen_urls: set[str] = set()
    for item in raw:
        try:
            article = _articles_adapter.validate_python([item])[0]
        except ValidationError as err:
            logger.info("websearch: dropping malformed result: %s", err)
            continue
        key = str(article.url)
        if key in seen_urls:
            continue
        if _host_key(key) in excluded:
            continue
        seen_urls.add(key)
        out.append(article)
        if len(out) >= max_results:
            break
    return out
