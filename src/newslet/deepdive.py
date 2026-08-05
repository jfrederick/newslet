"""Deep dives: reader-requested ~500-word explainers ("You asked").

The reader types a topic into the box on ``/``; the next digest build pops
the oldest pending request from the requests table, generates one
explainer with the main model, and opens the issue with it. One request
per issue; the request is marked served only after a confirmed send, so a
failed generation or failed send leaves it queued for the next build.

Not votable: this is content the reader explicitly asked for, so there is
no taste to learn.

Best-effort like every enrichment: any failure yields ``None`` and the
block is simply absent (with the request left pending).
"""

from __future__ import annotations

import json
import logging

import anthropic
from pydantic import ValidationError

from .config import settings
from .contracts import DeepDive
from .search_common import extract_json_object, last_text_block

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You write a requested explainer for a daily newsletter with one curious,
technically-literate reader. They asked for a deep dive on the topic below.

Rules:
- A one-line title (not just the topic restated).
- Roughly 500 words (450-550): concrete, accurate, self-contained. Explain
mechanisms, name the key ideas/people/numbers, and give the reader a real
mental model — not a survey of headings.
- Write from well-established knowledge. If the topic is ambiguous, pick
the most useful reading and say so in passing.
- Plain paragraphs separated by blank lines. No markdown headings, lists,
or code fences.

Reply with ONLY a JSON object (no prose, no markdown fences):

{"deepdive": {"title": "...", "body_md": "..."}}
"""


def fetch_deepdive(
    topic: str,
    *,
    client: anthropic.Anthropic | None = None,
    model: str | None = None,
) -> DeepDive | None:
    """Return the explainer for ``topic``, or ``None`` on any failure."""
    topic = topic.strip()
    if not topic:
        return None
    if client is None:
        client = anthropic.Anthropic(api_key=settings().anthropic_api_key)

    try:
        response = client.messages.create(
            model=model or settings().claude_model,
            max_tokens=2048,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"# Requested topic\n{topic}"}],
        )
    except Exception as exc:  # noqa: BLE001 - best effort; never raise
        logger.warning("deepdive: API call failed: %s", exc)
        return None

    stop_reason = getattr(response, "stop_reason", None)
    text = last_text_block(response.content)
    if text is None:
        logger.warning(
            "deepdive: no text block in response (stop_reason=%s)", stop_reason
        )
        return None
    json_str = extract_json_object(text)
    if json_str is None:
        logger.warning(
            "deepdive: no JSON object found (stop_reason=%s): %.200s",
            stop_reason,
            text,
        )
        return None
    try:
        # strict=False: multi-paragraph prose, same reasoning as facts.
        payload = json.loads(json_str, strict=False)
    except json.JSONDecodeError as err:
        logger.warning(
            "deepdive: could not parse response (stop_reason=%s): %s",
            stop_reason,
            err,
        )
        return None

    raw = payload.get("deepdive") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        logger.warning("deepdive: reply carried no deepdive object")
        return None
    try:
        return DeepDive.model_validate({**raw, "topic": topic})
    except ValidationError as err:
        logger.warning("deepdive: dropping malformed reply: %s", err)
        return None
