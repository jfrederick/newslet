"""Shared primitives for Claude server-side ``web_search`` calls.

Both :mod:`newslet.discovery` (sources outside your feeds) and
:mod:`newslet.websearch` (the "from around the web" block + the web view's
subject box) ask Claude to run a live web search and reply with a JSON object.
That shared shape needs the same handful of helpers:

- :func:`web_search_tool` — the server-side ``web_search`` tool definition.
- :func:`last_text_block` — the model's final JSON lands in the *last* text
  block, not ``content[0]``, because tool use interleaves tool-call/result
  blocks ahead of it.
- :func:`extract_json_object` — with the search tool active the model often
  ignores "ONLY a JSON object, no fences" and wraps its answer in a ```json
  fence or surrounding prose; this digs the object back out.
- :func:`host_key` — a lowercased, ``www.``-stripped host used to dedupe and
  to exclude already-followed domains.

Keeping them here (rather than reaching into one module's privates from the
other) gives both callers a single, directly-tested home.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# Server-side web search tool. Pin to the version string the API expects;
# tests never call the real API so the exact value is not load-bearing here.
_WEB_SEARCH_TOOL_TYPE = "web_search_20250305"


def web_search_tool(max_uses: int = 5) -> dict:
    """The server-side ``web_search`` tool, capped at ``max_uses`` rounds.

    ``max_uses`` is floored at 1: the interactive subject box may pass a low
    (or zero) cap to stay under the HTTP API's ~30s timeout, but the tool must
    always allow at least one search round rather than emit an invalid cap.
    """
    return {
        "type": _WEB_SEARCH_TOOL_TYPE,
        "name": "web_search",
        "max_uses": max(1, max_uses),
    }


def host_key(url: str) -> str:
    """Return the lowercased host of ``url`` without a leading ``www.``.

    This is a host-level backstop, not a true registered-domain (eTLD+1)
    extractor: ``news.bbc.co.uk`` and ``bbc.co.uk`` produce different keys.
    The primary "exclude sources the user already follows" rule is enforced
    by the model via the system prompt; this filter just catches exact-host
    repeats without pulling in a public-suffix dependency.
    """
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def last_text_block(content: list) -> str | None:
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


def extract_json_object(text: str) -> str | None:
    """Pull the JSON object out of a model reply.

    With the web_search tool active the model often ignores the
    "ONLY a JSON object, no fences" instruction and wraps its answer in a
    ```json fence, prefixes a sentence ("Here are the articles..."), or
    trails one ("...}\nHope that helps!"). A bare ``json.loads`` on any of
    those raises and — because these callers are best-effort — the section
    silently vanishes. Prefer the first fenced block that looks like an
    object, then return the first balanced ``{...}`` span, ignoring braces
    inside string literals so surrounding prose (or a stray brace in a
    title/reason) can't kill an otherwise-valid payload.
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
