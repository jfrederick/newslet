"""On-demand web search via Claude's server-side ``web_search`` tool.

Two consumers:

- The digest calls :func:`search_web` with a query distilled from the user's
  profile to fill the web view's "from around the web" block (the 20 articles
  pulled from the open web, separate from the RSS/HN picks).
- The web view's subject box ("type a subject, the search hones in") calls it
  live with whatever the user typed.

Best-effort throughout: a parse failure or empty model reply yields an empty
list rather than raising. The Anthropic client is injectable so tests never
touch the network. JSON extraction reuses :mod:`newslet.llm_parse` helpers,
which already tolerate the fences/prose the model emits when the search tool
is active.
"""

from __future__ import annotations

import logging

import anthropic
from pydantic import TypeAdapter, ValidationError

from .config import get_anthropic_client, settings
from .contracts import WebArticle
from .llm_parse import host_key as _host_key
from .llm_parse import parse_llm_json_response

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


def _variety_directive(variety: int) -> str:
    """Turn the 0–100 variety dial into an exploration instruction.

    Low values keep results tight to the user's stated interests; high values
    deliberately wander into *related, ancillary* areas — adjacent fields,
    second-order implications, neighbouring disciplines — that a curious
    reader would find enriching. It is exploratory, never random: every
    result must still connect back to the user's interests by a clear thread.
    """
    variety = max(0, min(100, variety))
    if variety <= 15:
        return (
            "Stay tightly on the user's stated interests; prefer the most "
            "directly relevant, on-topic results."
        )
    if variety <= 45:
        return (
            "Mostly match the user's stated interests, but include a few "
            "results from closely adjacent areas that connect clearly to them."
        )
    if variety <= 75:
        return (
            "Balance on-topic results with exploratory ones from related, "
            "ancillary areas — adjacent fields and second-order angles that "
            "broaden the user's interests without leaving their orbit."
        )
    return (
        "Emphasize exploratory results: venture into ancillary and adjacent "
        "areas — neighbouring disciplines, surprising connections, and "
        "second-order implications of the user's interests. Stay related and "
        "thematically connected; never random or off-topic."
    )


def _build_user_block(query: str, *, recent: bool, variety: int) -> str:
    recency = (
        "Focus on material published in roughly the last week.\n\n"
        if recent
        else ""
    )
    exploration = f"# Exploration\n{_variety_directive(variety)}\n\n"
    return f"{recency}{exploration}# Request\n{query.strip()}"


def search_web(
    query: str,
    *,
    max_results: int = 20,
    recent: bool = True,
    client: anthropic.Anthropic | None = None,
    exclude_hosts: list[str] | None = None,
    max_searches: int = 3,
    model: str | None = None,
    variety: int = 0,
) -> list[WebArticle]:
    """Search the open web for ``query`` and return up to ``max_results``.

    ``exclude_hosts`` drops results from hosts already covered elsewhere
    (e.g. the user's own feeds), de-duplicated by registered-ish host the
    same way :mod:`discovery` does. ``max_searches`` caps the tool rounds and
    ``model`` overrides the configured model — the interactive subject box
    passes a low cap and a fast model so it returns within the HTTP API's
    ~30s limit. ``variety`` (0–100) controls how far results may roam into
    related, ancillary areas (see :func:`_variety_directive`). Returns ``[]``
    on any failure.
    """
    if not query.strip():
        return []
    if client is None:
        client = get_anthropic_client()

    excluded = {_host_key(f"//{h}") or h.lower() for h in (exclude_hosts or [])}

    try:
        response = client.messages.create(
            model=model or settings().claude_model,
            max_tokens=2048,
            system=_SYSTEM_PROMPT.format(max_results=max_results),
            tools=[_web_search_tool(max_searches)],
            messages=[
                {
                    "role": "user",
                    "content": _build_user_block(
                        query, recent=recent, variety=variety
                    ),
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001 - best effort; never raise
        logger.warning("websearch: API call failed: %s", exc)
        return []

    payload = parse_llm_json_response(response.content, label="websearch")
    if payload is None:
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
