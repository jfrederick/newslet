"""Shared helpers for parsing JSON from Claude responses.

Both :mod:`discovery` and :mod:`websearch` ask Claude to return JSON objects
after using the server-side ``web_search`` tool. The response interleaves
tool-use and text blocks, and Claude frequently ignores the "no prose, no
fences" instruction. These utilities extract, parse, and validate that JSON
in a single reusable pipeline so both modules share one battle-tested path.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


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
    """Pull the first balanced JSON object out of a model reply.

    With the web_search tool active the model often ignores the
    "ONLY a JSON object, no fences" instruction and wraps its answer in a
    ``json`` fence, prefixes a sentence ("Here are the articles..."), or
    trails one ("...}\\nHope that helps!"). A bare ``json.loads`` on any of
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


def parse_llm_json_response(
    content: list,
    *,
    label: str = "llm",
) -> dict | None:
    """Extract and parse a JSON object from a Claude response's content blocks.

    Returns the parsed ``dict`` on success, or ``None`` on any failure (no text
    block, no JSON found, or invalid JSON). All failures are logged under
    ``label`` for diagnostics.
    """
    text = last_text_block(content)
    if text is None:
        logger.warning("%s: no text block in response", label)
        return None

    json_str = extract_json_object(text)
    if json_str is None:
        logger.warning("%s: no JSON object found: %.200s", label, text)
        return None

    try:
        return json.loads(json_str)
    except json.JSONDecodeError as err:
        logger.warning("%s: could not parse response: %s", label, err)
        return None
