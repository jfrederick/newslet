"""Tech facts: two ~500-word essays per issue, from model knowledge alone.

The methodology lives here:

- A fixed taxonomy of eight genres (``GENRES``). Each issue gets two facts
  from two *different* genres, chosen by the model but steered by the
  reader's facts-taste profile (``id="facts"`` row — see
  :func:`newslet.db.get_facts_state`), which is tuned only by votes on
  facts (:func:`tune_facts_profile`), never by the general profile tuner.
- A rolling no-repeat log: the digest passes the last ~60 covered topics
  and the prompt excludes them.
- Facts must be timeless (no news pegs) and verifiable — this module makes
  a plain model call with no web_search tool, which keeps it cheap and
  steers the model toward durable, well-established material.

Best-effort throughout, like every enrichment source: any failure (API
error, unparsable reply, half a result) yields ``[]``/unchanged input
rather than raising, so facts can never block the send.
"""

from __future__ import annotations

import json
import logging
import re

import anthropic
from pydantic import ValidationError

from .config import settings
from .contracts import Fact, FeedbackRow
from .search_common import extract_json_object, last_text_block

logger = logging.getLogger(__name__)

# The synthetic vote-URL path shape email_render mints for fact votes:
# /facts/{issue-key}/{slot}, where the issue key is a date (YYYY-MM-DD) or a
# manual-send key. Anchored and fully validated so a real article that
# happens to contain ".../facts/..." in its path never matches. Shared by
# handlers.digest (feedback routing) and handlers.web (/rate title lookup +
# thanks page) so the two can never drift apart.
_ISSUE_KEY_RE = r"(?:\d{4}-\d{2}-\d{2}|manual-[0-9a-zA-Z-]+)"
VOTE_PATH_RE = re.compile(rf"^/facts/{_ISSUE_KEY_RE}/(mid|end)$")


def vote_slot(path: str) -> str | None:
    """The fact slot ("mid"/"end") for a synthetic vote-URL path, else None."""
    m = VOTE_PATH_RE.match(path)
    return m.group(1) if m else None


GENRES = (
    "computing history & lore",
    "how-it-works internals",
    "people & personalities",
    "hardware & physics of computing",
    "networks & protocols",
    "algorithms & math",
    "security & cryptography",
    "software culture & economics",
)

_SYSTEM_PROMPT = """\
You write the two "tech fact" essays for a daily technology newsletter with \
one discerning reader.

Genres (pick TWO DIFFERENT ones for the two essays):
{genres}

Use the reader's facts-taste profile below to weight your genre and topic \
choices, but keep variety day to day — lean toward what they enjoy without \
repeating the same genre pairing every issue. If the profile is empty, pick \
any two genres.

Rules for each essay:
- A one-line, curiosity-piquing title (no clickbait).
- Roughly 500 words (450-550): concrete, technically accurate, and \
self-contained. Explain mechanisms and give real numbers, names, and dates \
where they matter.
- TIMELESS: no news, no "recently"/"last week", nothing that will read as \
stale in a year. Well-established material only — if you are not certain a \
claim is true, use a different topic.
- Plain paragraphs separated by blank lines. No markdown headings, lists, \
or code fences.
- Avoid every topic in the exclusion list (already covered on recent days).

Reply with ONLY a JSON object (no prose, no markdown fences):

{{
  "facts": [
    {{"title": "...", "body_md": "...", "genre": "<one of the genres>", "slot": "mid"}},
    {{"title": "...", "body_md": "...", "genre": "<a different genre>", "slot": "end"}}
  ]
}}
"""

_TUNE_SYSTEM_PROMPT = """\
You maintain a short profile of which TECH FACTS a newsletter reader enjoys.

You are given your current understanding (may be empty) and the latest \
up/down votes on individual fact essays (titles included). Produce an \
UPDATED understanding: which genres, eras, topics, and styles they like or \
dislike. Keep observations that still hold, fold in the new signal, revise \
what the votes contradict.

Reply with ONLY a markdown bullet list (``-`` bullets, no heading, no \
prose). At most ten bullets.
"""


def _user_block(facts_profile_md: str, recent_topics: list[str]) -> str:
    profile = facts_profile_md.strip() or "(none yet — pick any two genres)"
    exclusions = (
        "\n".join(f"- {t}" for t in recent_topics) if recent_topics else "(none)"
    )
    return (
        "# Reader's facts-taste profile\n"
        f"{profile}\n\n"
        "# Exclusion list — topics already covered recently\n"
        f"{exclusions}"
    )


def fetch_facts(
    facts_profile_md: str,
    recent_topics: list[str],
    *,
    client: anthropic.Anthropic | None = None,
    model: str | None = None,
) -> list[Fact]:
    """Return exactly two facts (slots ``mid`` + ``end``), or ``[]``.

    All-or-nothing: a reply with one usable fact, duplicate slots, or any
    parse/API failure returns ``[]`` — a half result would render one slot
    and silently blank the other.
    """
    if client is None:
        client = anthropic.Anthropic(api_key=settings().anthropic_api_key)

    try:
        response = client.messages.create(
            model=model or settings().claude_model,
            max_tokens=4096,
            system=_SYSTEM_PROMPT.format(genres="\n".join(f"- {g}" for g in GENRES)),
            messages=[
                {
                    "role": "user",
                    "content": _user_block(facts_profile_md, recent_topics),
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001 - best effort; never raise
        logger.warning("facts: API call failed: %s", exc)
        return []

    stop_reason = getattr(response, "stop_reason", None)
    text = last_text_block(response.content)
    if text is None:
        logger.warning("facts: no text block in response (stop_reason=%s)", stop_reason)
        return []
    json_str = extract_json_object(text)
    if json_str is None:
        logger.warning(
            "facts: no JSON object found (stop_reason=%s): %.200s", stop_reason, text
        )
        return []
    try:
        # strict=False: two ~500-word essays are the first multi-paragraph
        # prose through this JSON contract, and a model reply with a literal
        # control character inside a string would otherwise cost the reader
        # both fact blocks under the all-or-nothing rule.
        payload = json.loads(json_str, strict=False)
    except json.JSONDecodeError as err:
        logger.warning(
            "facts: could not parse response (stop_reason=%s): %s", stop_reason, err
        )
        return []

    raw = payload.get("facts", []) if isinstance(payload, dict) else []
    out: list[Fact] = []
    for item in raw:
        try:
            out.append(Fact.model_validate(item))
        except ValidationError as err:
            logger.warning("facts: dropping malformed fact: %s", err)
    if len(out) != 2 or {f.slot for f in out} != {"mid", "end"}:
        logger.warning(
            "facts: need exactly one mid + one end fact, got %d (%s); dropping",
            len(out),
            [f.slot for f in out],
        )
        return []
    return out


def tune_facts_profile(
    current_md: str,
    feedback: list[FeedbackRow],
    *,
    client: anthropic.Anthropic | None = None,
) -> str:
    """Rewrite the facts-taste profile from fact votes.

    The whole markdown is auto-managed (no human-written portion, unlike
    ``tune.tune_profile``). Returns ``current_md`` unchanged on empty
    feedback or any API failure.
    """
    if not feedback:
        return current_md
    if client is None:
        client = anthropic.Anthropic(api_key=settings().anthropic_api_key)

    votes = "\n".join(
        f'{"+" if row.rating == "up" else "-"} {row.title or row.article_url}'
        + (f" — note: {row.note}" if row.note else "")
        for row in feedback
    )
    user_block = (
        "# Current understanding\n"
        f"{current_md.strip() or '(none yet)'}\n\n"
        "# Latest fact votes\n"
        f"{votes}"
    )
    try:
        response = client.messages.create(
            model=settings().claude_model,
            max_tokens=1024,
            system=_TUNE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_block}],
        )
        text = last_text_block(response.content)
    except Exception as exc:  # noqa: BLE001 - best effort; never raise
        logger.warning("facts: tune call failed: %s", exc)
        return current_md
    if not text or not text.strip():
        return current_md
    return text.strip()
