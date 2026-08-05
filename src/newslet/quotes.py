"""Quote of the day: one philosophical quote per issue, as an epigraph.

Philosophy, not STEM trivia: Stoics (Marcus Aurelius, Seneca, Epictetus),
Nietzsche, Einstein's reflective remarks, Buddhist and Taoist texts, and
kin (Montaigne, Kierkegaard, Camus, Thoreau, Rumi, Confucius, ...). The
prompt demands real, attributable quotes — author and source — and rotates
traditions, weighted by a quotes-taste profile (``id="quotes"`` row) that
is tuned only by votes on quotes (:func:`tune_quotes_profile`), never by
the general profile tuner. A rolling no-repeat log keeps quotes from
recurring.

Mirrors :mod:`newslet.facts` exactly: plain model call (Haiku by default —
quotes are short and canonical), best-effort ``None`` on any failure, and
an anchored synthetic vote-URL shape shared by the digest's feedback
routing and the web handler.
"""

from __future__ import annotations

import json
import logging
import re

import anthropic
from pydantic import ValidationError

from .config import settings
from .contracts import FeedbackRow, Quote
from .search_common import extract_json_object, last_text_block

logger = logging.getLogger(__name__)

# The synthetic vote-URL path shape email_render mints for quote votes:
# /quote/{issue-key}. Anchored and fully validated (issue key is a date or
# manual-send key) so a real article at e.g. example.com/quote/stoicism
# never matches. Shared by handlers.digest and handlers.web.
_ISSUE_KEY_RE = r"(?:\d{4}-\d{2}-\d{2}|manual-[0-9a-zA-Z-]+)"
VOTE_PATH_RE = re.compile(rf"^/quote/{_ISSUE_KEY_RE}$")


def is_quote_vote(path: str) -> bool:
    """Whether a vote-URL path is the app's synthetic quote shape."""
    return VOTE_PATH_RE.match(path) is not None


# Quotes are short, canonical text — the fast model is plenty, and this
# runs every single day.
_QUOTE_MODEL = "claude-haiku-4-5-20251001"

_SYSTEM_PROMPT = """\
You choose one quote of the day for a thoughtful newsletter reader.

Philosophy and wisdom, not STEM trivia. Traditions to rotate between:
- Stoics: Marcus Aurelius, Seneca, Epictetus
- Nietzsche
- Einstein's reflective, philosophical remarks
- Buddhist texts and teachers
- Taoist texts: Laozi, Zhuangzi
- Kin: Montaigne, Kierkegaard, Camus, Thoreau, Rumi, Confucius, and similar

Rules:
- The quote must be REAL and attributable: give the author and the work or
  context it comes from. If you are not certain a quote is genuine, choose
  a different one you are certain of. Never invent or paraphrase-and-quote.
- Use the reader's quotes-taste profile below to weight tradition and theme
  choices, but keep variety day to day. Empty profile: pick freely.
- Avoid every entry in the exclusion list (recently shown).
- Keep it short — a line or three, not a paragraph.

Reply with ONLY a JSON object (no prose, no markdown fences):

{
  "quote": {
    "text": "...",
    "author": "...",
    "source": "<work or context>",
    "tradition": "<e.g. Stoic, Taoist, Nietzsche>"
  }
}
"""

_TUNE_SYSTEM_PROMPT = """\
You maintain a short profile of which QUOTES a newsletter reader enjoys.

You are given your current understanding (may be empty) and the latest
up/down votes on individual quotes (author + text prefix included).
Produce an UPDATED understanding: which traditions, authors, themes, and
tones they like or dislike. Keep observations that still hold, fold in the
new signal, revise what the votes contradict.

Reply with ONLY a markdown bullet list (``-`` bullets, no heading, no
prose). At most eight bullets.
"""


def _user_block(quotes_profile_md: str, recent_quotes: list[str]) -> str:
    profile = quotes_profile_md.strip() or "(none yet — pick freely)"
    exclusions = (
        "\n".join(f"- {q}" for q in recent_quotes) if recent_quotes else "(none)"
    )
    return (
        "# Reader's quotes-taste profile\n"
        f"{profile}\n\n"
        "# Exclusion list — quotes already shown recently\n"
        f"{exclusions}"
    )


def fetch_quote(
    quotes_profile_md: str,
    recent_quotes: list[str],
    *,
    client: anthropic.Anthropic | None = None,
    model: str | None = None,
) -> Quote | None:
    """Return one quote, or ``None`` on any failure (best-effort)."""
    if client is None:
        client = anthropic.Anthropic(api_key=settings().anthropic_api_key)

    try:
        response = client.messages.create(
            model=model or _QUOTE_MODEL,
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": _user_block(quotes_profile_md, recent_quotes),
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001 - best effort; never raise
        logger.warning("quotes: API call failed: %s", exc)
        return None

    stop_reason = getattr(response, "stop_reason", None)
    text = last_text_block(response.content)
    if text is None:
        logger.warning(
            "quotes: no text block in response (stop_reason=%s)", stop_reason
        )
        return None
    json_str = extract_json_object(text)
    if json_str is None:
        logger.warning(
            "quotes: no JSON object found (stop_reason=%s): %.200s", stop_reason, text
        )
        return None
    try:
        # strict=False for the same reason as facts: a literal control
        # character inside the quote text must not cost the block.
        payload = json.loads(json_str, strict=False)
    except json.JSONDecodeError as err:
        logger.warning(
            "quotes: could not parse response (stop_reason=%s): %s", stop_reason, err
        )
        return None

    raw = payload.get("quote") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        logger.warning("quotes: reply carried no quote object")
        return None
    try:
        return Quote.model_validate(raw)
    except ValidationError as err:
        logger.warning("quotes: dropping malformed quote: %s", err)
        return None


def tune_quotes_profile(
    current_md: str,
    feedback: list[FeedbackRow],
    *,
    client: anthropic.Anthropic | None = None,
) -> str:
    """Rewrite the quotes-taste profile from quote votes.

    Wholly auto-managed like the facts profile; returns ``current_md``
    unchanged on empty feedback or any API failure.
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
        "# Latest quote votes\n"
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
        logger.warning("quotes: tune call failed: %s", exc)
        return current_md
    if not text or not text.strip():
        return current_md
    return text.strip()
