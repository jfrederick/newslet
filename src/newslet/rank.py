"""Rank candidate articles using Claude.

The :func:`rank` function wraps the Anthropic Messages API call that scores
RSS candidates against the user's profile + recent feedback.  Prompt caching
is applied to the stable (profile + feedback) block so the high-churn
candidate list can change daily without invalidating the cache.
"""

from __future__ import annotations

import json
import logging

import anthropic
from pydantic import ValidationError

from .config import settings
from .contracts import Article, FeedbackRow, RankResponse

log = logging.getLogger(__name__)

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


# Each pick serializes to a JSON object with a url, title, one-sentence blurb,
# source, and score. ``max_tokens`` must hold the whole array or the reply is
# truncated into invalid JSON — which fails to parse, fails the retry the same
# way, and raises, aborting the caller. The daily email asks for ~10 picks (fits
# 4096), but the homepage asks for 25-40 (see digest._HOME_RANK_PICKS), so a
# fixed 4096 silently broke the homepage rebuild. Size the budget to the request
# instead: a generous per-pick allowance (long URLs + pretty-printed JSON
# inflate it), a floor that preserves the email's behavior, and a cap that stays
# under the SDK's non-streaming timeout guard (~16k).
_RANK_TOKENS_PER_PICK = 300
_RANK_MIN_OUTPUT_TOKENS = 4096
_RANK_MAX_OUTPUT_TOKENS = 16000


def _rank_output_tokens(max_picks: int) -> int:
    """Output-token budget scaled to the requested pick count."""
    budget = 1024 + max_picks * _RANK_TOKENS_PER_PICK
    return max(_RANK_MIN_OUTPUT_TOKENS, min(_RANK_MAX_OUTPUT_TOKENS, budget))


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
    output_tokens = _rank_output_tokens(max_picks)

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
        max_tokens=output_tokens,
        system=system_prompt,
        messages=messages,
    )
    if getattr(response, "stop_reason", None) == "max_tokens":
        # Truncated mid-array → the JSON below won't parse. Name it: this was the
        # silent cause of stale homepage rebuilds when the budget was fixed.
        log.warning(
            "rank: hit max_tokens (%d) for max_picks=%d; reply likely truncated",
            output_tokens,
            max_picks,
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
            max_tokens=output_tokens,
            system=system_prompt,
            messages=messages,
        )
        retry_text = retry.content[0].text
        try:
            parsed = RankResponse.model_validate_json(retry_text)
        except ValidationError:
            raise first_err from None

    # Ground the picks to the candidate pool. The model can otherwise echo an
    # article from its own training data — a plausible-looking but *stale* story
    # that was never in today's fetched candidates — which would bypass every
    # upstream freshness filter (the 24h RSS window, HN's recency cap). Keep
    # only picks whose URL is one we actually supplied.
    candidate_urls = {str(c.url) for c in candidates}
    grounded = [p for p in parsed.picks if str(p.url) in candidate_urls]
    dropped = [p for p in parsed.picks if str(p.url) not in candidate_urls]
    if dropped:
        # Log the offending URLs, not just a count: when a stale article does
        # leak through the model, this line names it so the next forensic pass
        # is immediate rather than a repro.
        log.warning(
            "rank: dropped %d ungrounded pick(s) not in the candidate pool: %s",
            len(dropped),
            [str(p.url) for p in dropped],
        )

    top = sorted(grounded, key=lambda p: p.score, reverse=True)[:max_picks]
    return RankResponse(picks=top)
