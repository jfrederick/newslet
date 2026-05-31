"""Rank candidate articles using Claude.

The :func:`rank` function wraps the Anthropic Messages API call that scores
RSS candidates against the user's profile + recent feedback.  Prompt caching
is applied to the stable (profile + feedback) block so the high-churn
candidate list can change daily without invalidating the cache.
"""

from __future__ import annotations

import json

import anthropic
from pydantic import ValidationError

from .config import settings
from .contracts import Article, FeedbackRow, RankResponse

_SYSTEM_PROMPT = """\
You rank candidate news articles for a personalized daily email digest.

Score each article 0.0-1.0 based on how well it matches the user's profile
and recent feedback.  Write a one-sentence ``blurb`` for each pick.

Aim to return at least {min_picks} picks and at most {max_picks}.  Return
fewer than {min_picks} only if there genuinely aren't that many relevant
candidates to choose from.

Reply with ONLY a JSON object matching this schema (no prose, no markdown
fences):

{{
  "picks": [
    {{
      "url":    "<article url>",
      "title":  "<article title>",
      "blurb":  "<one-sentence synopsis>",
      "source": "<feed source>",
      "score":  <float between 0.0 and 1.0>
    }}
  ]
}}
"""


def _build_system_prompt(min_picks: int, max_picks: int) -> str:
    """Render the system prompt with the target pick-count window."""
    return _SYSTEM_PROMPT.format(min_picks=min_picks, max_picks=max_picks)


def _build_stable_block(profile_md: str, feedback: list[FeedbackRow]) -> str:
    """Format the profile + feedback into the cache-stable user block."""
    lines = ["# User profile", profile_md.strip(), "", "# Recent feedback"]
    if not feedback:
        lines.append("(none yet)")
    else:
        for row in feedback:
            sign = "+" if row.rating == "up" else "-"
            line = f'{sign} {row.article_url} "{row.title}"'
            if row.note:
                line += f" — note: {row.note}"
            lines.append(line)
    return "\n".join(lines)


def _format_candidates(candidates: list[Article]) -> str:
    """Format today's candidates as a compact JSON array literal."""
    payload = [
        {
            "url": str(c.url),
            "title": c.title,
            "summary": c.summary,
            "source": c.source,
        }
        for c in candidates
    ]
    return "# Today's candidates\n" + json.dumps(payload, ensure_ascii=False)


def rank(
    profile_md: str,
    feedback: list[FeedbackRow],
    candidates: list[Article],
    *,
    client: anthropic.Anthropic | None = None,
    min_picks: int = 5,
    max_picks: int = 10,
) -> RankResponse:
    """Ask Claude to rank ``candidates`` and return the top ``max_picks``.

    ``min_picks`` is a soft floor: the model is asked to surface at least that
    many picks (padding a quiet news day with lower-scoring items) but may
    return fewer if there genuinely aren't enough relevant candidates.

    On a :class:`pydantic.ValidationError` from the first reply, one retry
    is attempted with a follow-up nudge.  If the retry also fails to parse,
    the *original* ``ValidationError`` is raised.
    """
    if client is None:
        client = anthropic.Anthropic(api_key=settings().anthropic_api_key)

    system_prompt = _build_system_prompt(min_picks, max_picks)
    stable_block = _build_stable_block(profile_md, feedback)
    candidates_block = _format_candidates(candidates)

    messages: list[dict] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": stable_block,
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": candidates_block},
            ],
        }
    ]

    model = settings().claude_model
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system_prompt,
        messages=messages,
    )
    text = response.content[0].text

    try:
        parsed = RankResponse.model_validate_json(text)
    except ValidationError as first_err:
        messages.append({"role": "assistant", "content": text})
        messages.append(
            {
                "role": "user",
                "content": (
                    "Your previous reply was not valid JSON matching the "
                    "schema. Reply with ONLY the JSON object, nothing else."
                ),
            }
        )
        retry = client.messages.create(
            model=model,
            max_tokens=4096,
            system=system_prompt,
            messages=messages,
        )
        retry_text = retry.content[0].text
        try:
            parsed = RankResponse.model_validate_json(retry_text)
        except ValidationError:
            raise first_err from None

    top = sorted(parsed.picks, key=lambda p: p.score, reverse=True)[:max_picks]
    return RankResponse(picks=top)
