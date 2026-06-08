"""Auto-tune the user profile from recent feedback using Claude.

The user hand-writes the top of their profile.  :func:`tune_profile` keeps a
single delimited, auto-managed "Learned preferences" section at the bottom.
The summary is *cumulative*: each run feeds the existing learned block back to
Claude as its current understanding and asks it to merge the latest feedback in
— keeping observations that still hold, folding in new signal, and revising
anything the new votes contradict.  So insight survives even after the raw
votes that produced it age out of the feedback window.  The human-written
portion above the sentinels is always preserved verbatim, and the function
never raises: on an empty feedback list or any Claude error it returns the
input markdown unchanged.
"""

from __future__ import annotations

import anthropic

from .config import get_anthropic_client, settings
from .contracts import FeedbackRow, format_feedback_line

_BLOCK_START = "<!-- learned-preferences:auto:start -->"
_BLOCK_END = "<!-- learned-preferences:auto:end -->"

_SYSTEM_PROMPT = """\
You maintain the "Learned preferences" section of a user's news-digest profile.

You are given:
1. The user's hand-written profile.
2. Your current learned-preferences summary (may be empty on the first run).
3. The latest batch of feedback (up/down votes plus optional free-text notes).

Produce an UPDATED learned-preferences summary that builds on the current one:
keep observations that still hold, fold in the new feedback, and revise or drop
anything the new votes contradict.  This is a running, cumulative understanding
— do not discard prior insight just because it is absent from the latest batch.

Reply with ONLY the bullet list (markdown ``-`` bullets, no heading, no prose,
no fences).  Keep it tight: about a dozen bullets at most.
"""


def _strip_block(profile_md: str) -> str:
    """Remove an existing auto-managed block (and its sentinels) if present.

    Returns the human-written portion with trailing whitespace trimmed.  If no
    block is found, the input is returned unchanged (minus trailing whitespace).
    """
    start = profile_md.find(_BLOCK_START)
    if start == -1:
        return profile_md.rstrip()
    end = profile_md.find(_BLOCK_END, start)
    if end == -1:
        # Malformed (start without end): drop from the start sentinel onward.
        return profile_md[:start].rstrip()
    human = profile_md[:start] + profile_md[end + len(_BLOCK_END):]
    return human.rstrip()


def _extract_block(profile_md: str) -> str:
    """Return the inner bullet summary of an existing auto block, or ''.

    Drops the sentinels and the ``## Learned preferences (auto)`` heading so the
    bare bullets can be fed back to Claude as the current understanding.
    """
    start = profile_md.find(_BLOCK_START)
    if start == -1:
        return ""
    end = profile_md.find(_BLOCK_END, start)
    inner = (
        profile_md[start + len(_BLOCK_START):]
        if end == -1
        else profile_md[start + len(_BLOCK_START):end]
    )
    lines = [
        ln
        for ln in inner.splitlines()
        if ln.strip() and not ln.strip().startswith("## Learned preferences")
    ]
    return "\n".join(lines).strip()


def _format_feedback(feedback: list[FeedbackRow]) -> str:
    """Format feedback rows (rating + note) into a compact prompt block."""
    return "\n".join(
        format_feedback_line(row, f"{row.title} ({row.article_url})")
        for row in feedback
    )


def _build_block(summary: str) -> str:
    """Wrap the Claude-written ``summary`` in the sentinel-delimited block."""
    return (
        f"{_BLOCK_START}\n"
        "## Learned preferences (auto)\n"
        f"{summary.strip()}\n"
        f"{_BLOCK_END}"
    )


def tune_profile(
    profile_md: str,
    feedback: list[FeedbackRow],
    *,
    client: anthropic.Anthropic | None = None,
) -> str:
    """Update the cumulative auto-managed preferences block from ``feedback``.

    Feeds the existing auto block back to Claude as the current understanding,
    asks it to merge ``feedback`` in, and re-appends a single delimited block.
    The human-written portion is preserved exactly.  Returns ``profile_md``
    unchanged when ``feedback`` is empty or the Claude call fails.
    """
    if not feedback:
        return profile_md

    if client is None:
        client = get_anthropic_client()

    human = _strip_block(profile_md)
    existing = _extract_block(profile_md)
    user_block = (
        "# User profile\n"
        f"{human}\n\n"
        "# Current learned preferences\n"
        f"{existing or '(none yet)'}\n\n"
        "# Latest feedback\n"
        f"{_format_feedback(feedback)}"
    )

    try:
        response = client.messages.create(
            model=settings().claude_model,
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_block}],
        )
        summary = response.content[0].text
    except Exception:
        return profile_md

    block = _build_block(summary)
    if human:
        return f"{human}\n\n{block}\n"
    return f"{block}\n"
