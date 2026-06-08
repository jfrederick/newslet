"""Summarize a day's picks into a humanized subject line and TL;DR intro.

The :func:`summarize_issue` function wraps a single Anthropic Messages API
call that, given the day's picks (titles + blurbs), returns a short, specific
email subject and a 2-3 sentence intro tying the picks together.  The system
prompt carries an explicit anti-AI-writing instruction block so the output
reads like a sharp human editor wrote it, not a chatbot.

On any failure (network, parse, validation) the function returns ``("", "")``
and never raises, letting the caller fall back to its own defaults.
"""

from __future__ import annotations

import json
import logging

import anthropic

from .config import settings
from .contracts import Pick

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You write the subject line and TL;DR intro for a personalized daily news
email digest.

Given the day's picks (titles + one-line blurbs), produce:
- ``subject``: a short, specific email subject derived from the actual
  content of today's picks. NOT a date. NOT generic ("Your daily digest").
  Name the most interesting concrete thing in today's picks.
- ``intro``: a 2-3 sentence TL;DR that ties the picks together and tells
  the reader what is worth their time today.

WRITE LIKE A SHARP HUMAN EDITOR. Plain, direct, specific phrasing. Avoid the
tells of AI writing:
- No significance inflation ("stands as a testament", "pivotal",
  "underscores", "marks a turning point").
- No copula avoidance ("serves as", "boasts", "features"); just say what is.
- No em-dash overuse. Prefer periods and commas.
- No rule-of-three cadence (three parallel items for rhythm).
- No AI vocabulary ("delve", "vibrant", "landscape", "tapestry",
  "additionally", "moreover", "navigate", "realm").
- No negative parallelisms ("not just X, but Y").
- No promotional fluff and no generic upbeat conclusions.
Use straight quotes. No emojis. Be concrete and name real things.

Reply with ONLY a JSON object matching this schema (no prose, no markdown
fences):

{
  "subject": "<short specific subject>",
  "intro":   "<2-3 sentence TL;DR>"
}
"""


def _format_picks(picks: list[Pick]) -> str:
    """Format today's picks as a compact JSON array literal."""
    payload = [
        {
            "title": p.title,
            "blurb": p.blurb,
            "source": p.source,
        }
        for p in picks
    ]
    return "# Today's picks\n" + json.dumps(payload, ensure_ascii=False)


def summarize_issue(
    picks: list[Pick],
    *,
    client: anthropic.Anthropic | None = None,
) -> tuple[str, str]:
    """Return a ``(subject, intro)`` pair summarizing today's ``picks``.

    Makes one Anthropic call and parses a ``{"subject", "intro"}`` JSON
    object from the reply.  Any failure (network, parse, missing keys)
    yields ``("", "")`` so the caller can fall back; this never raises.
    """
    if client is None:
        client = anthropic.Anthropic(api_key=settings().anthropic_api_key)

    picks_block = _format_picks(picks)

    try:
        response = client.messages.create(
            model=settings().claude_model,
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": picks_block}],
        )
        text = response.content[0].text
        parsed = json.loads(text)
        subject = parsed["subject"]
        intro = parsed["intro"]
    except Exception:  # noqa: BLE001 - safe fallback, never raise
        logger.warning("summarize failed", exc_info=True)
        return ("", "")

    if not isinstance(subject, str) or not isinstance(intro, str):
        return ("", "")
    return (subject, intro)
