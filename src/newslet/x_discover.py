"""Discover X (Twitter) *accounts* worth following, via Grok's ``x_search``.

The sibling :mod:`newslet.x_grok` pulls individual posts into the daily ranking
pool; this module instead surfaces whole **accounts** the user might want to
follow, each with a few recent teaser posts, for the admin "discover" page.

Shape and contract mirror :mod:`newslet.x_grok`:

- The only network edge is an injected ``complete`` callable (a single xAI
  Responses request → parsed JSON), so tests stay offline and we pull in no SDK.
- Best-effort throughout: a missing ``XAI_API_KEY``, a network/parse error, or
  an empty reply yields ``[]`` rather than raising — discovery must never break
  a page or a background refresh.
- Recency lives in the ``x_search`` tool's ``from_date`` (the past ``days``),
  and is double-checked against any per-post timestamp the model returns.

See https://docs.x.ai/developers/tools/x-search for the Agent Tools API.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta

from pydantic import ValidationError

from .config import settings
from .contracts import XAccount, XPost
from .search_common import extract_json_object

logger = logging.getLogger(__name__)

_XAI_ENDPOINT = "https://api.x.ai/v1/responses"
_REQUEST_TIMEOUT = 30
_USER_AGENT = "newslet/1.0 (+https://github.com/jfrederick/newslet)"

# Default recency window: an account must have posted within this many days,
# and every teaser post shown must fall inside it too.
_RECENCY_DAYS = 14

# Generous budget: the agentic x_search tool reasons + makes several tool
# rounds before emitting JSON for ~30 accounts, so this is larger than the
# post-only source's (see x_grok._MAX_OUTPUT_TOKENS for the same reasoning).
_MAX_OUTPUT_TOKENS = 16384

# Teaser posts per account, steered via the prompt and enforced on slice.
_MAX_POSTS_PER_ACCOUNT = 3

# The x_search tool is agentic (no result-cap parameter), so account/post
# counts are steered through the prompt and enforced when we parse the output.
_PROMPT = """\
Use X search to find up to {max_results} high-signal X (Twitter) ACCOUNTS a \
reader with the interests below would want to FOLLOW. Prefer substantive \
accounts — analysts, primary-source reporters, researchers, expert \
practitioners — over memes, engagement-bait, brands, and ads.

Hard requirements for every account you return:
- It must have posted ORIGINAL content within the last {days} days (active, \
not dormant).
- Include {min_posts}-{max_posts} of its most interesting RECENT posts (each \
from within the last {days} days).
{exclude_clause}
# Interests
{query}

# Output
Reply with ONLY a JSON object (no prose, no markdown fences) matching this \
schema:
{{"accounts": [{{"handle": "@username", "name": "Display Name", \
"bio": "one-line bio", "reason": "why this reader would want to follow them", \
"posts": [{{"url": "https://x.com/username/status/<id>", "text": "post text", \
"posted_at": "YYYY-MM-DD", "likes": 0, "reposts": 0}}]}}]}}
"""


def _default_complete(payload: dict, api_key: str) -> dict:
    """POST ``payload`` to the xAI Responses endpoint and parse the reply.

    Production path; tests inject a fake ``complete`` instead. Mirrors
    :func:`newslet.x_grok._default_complete`.
    """
    from urllib.request import Request, urlopen

    body = json.dumps(payload).encode("utf-8")
    req = Request(  # noqa: S310 - https only, constant endpoint
        _XAI_ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        },
    )
    with urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:  # noqa: S310 - https only
        return json.loads(resp.read().decode("utf-8"))


def _output_text(response: dict) -> str | None:
    """Pull the final assistant text out of a Responses-API reply.

    Identical shape-handling to :func:`newslet.x_grok._output_text`: prefer the
    convenience ``output_text`` aggregate, else concatenate the text blocks of
    the ``message`` item, degrading to ``None`` on any unexpected shape.
    """
    if not isinstance(response, dict):
        return None
    aggregate = response.get("output_text")
    if isinstance(aggregate, str) and aggregate.strip():
        return aggregate
    output = response.get("output")
    if not isinstance(output, list):
        return None
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "\n".join(parts) if parts else None


def _normalize_handle(raw: str) -> str:
    """Bare, lowercased username from a handle, @handle, or profile URL."""
    handle = str(raw or "").strip()
    if handle.startswith("http://") or handle.startswith("https://"):
        # Take the first path segment of an x.com/twitter.com profile URL.
        handle = handle.rstrip("/").rsplit("/", 1)[-1]
    return handle.lstrip("@").strip().lower()


def _parse_date(value: object) -> datetime | None:
    """Best-effort parse of a model-supplied ``posted_at`` (date or datetime)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.combine(date.fromisoformat(value.strip()[:10]), datetime.min.time())
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _post_from(raw: dict, *, cutoff: datetime) -> XPost | None:
    """Build an :class:`XPost`, dropping anything without a url or too old.

    A post with a parseable ``posted_at`` before ``cutoff`` is rejected; a
    missing/garbled timestamp is kept (the tool's ``from_date`` already bounds
    recency, so we don't punish the model for omitting a date).
    """
    if not isinstance(raw, dict):
        return None
    url = raw.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    posted_at = _parse_date(raw.get("posted_at"))
    if posted_at is not None and posted_at < cutoff:
        return None
    try:
        return XPost(
            url=url.strip(),
            text=" ".join(str(raw.get("text") or "").split()),
            posted_at=posted_at,
            likes=raw.get("likes") if isinstance(raw.get("likes"), int) else None,
            reposts=raw.get("reposts") if isinstance(raw.get("reposts"), int) else None,
        )
    except ValidationError as exc:
        logger.info("x-discover: skipping unusable post %s: %s", url, exc)
        return None


def _account_from(raw: dict, *, cutoff: datetime) -> XAccount | None:
    """Build an :class:`XAccount` from one Grok account object.

    Returns ``None`` (and logs) when the account has no usable handle or no
    recent posts — an account we can't show a fresh teaser for isn't a useful
    discovery.
    """
    if not isinstance(raw, dict):
        return None
    handle = _normalize_handle(raw.get("handle") or "")
    if not handle:
        return None
    posts: list[XPost] = []
    for item in raw.get("posts") or []:
        post = _post_from(item, cutoff=cutoff)
        if post is not None:
            posts.append(post)
        if len(posts) >= _MAX_POSTS_PER_ACCOUNT:
            break
    if not posts:
        return None
    try:
        return XAccount(
            handle=handle,
            name=str(raw.get("name") or "").strip(),
            bio=" ".join(str(raw.get("bio") or "").split()),
            url=f"https://x.com/{handle}",
            reason=" ".join(str(raw.get("reason") or "").split()),
            posts=posts,
        )
    except ValidationError as exc:
        logger.info("x-discover: skipping unusable account @%s: %s", handle, exc)
        return None


def find_x_accounts(
    query: str,
    *,
    exclude_handles: Iterable[str] = (),
    max_results: int = 30,
    days: int = _RECENCY_DAYS,
    api_key: str | None = None,
    model: str | None = None,
    complete: Callable[[dict, str], dict] | None = None,
    now: datetime | None = None,
) -> list[XAccount]:
    """Return up to ``max_results`` followable X accounts, best-effort.

    ``query`` is a profile-distilled interests string (the digest reuses its
    ``_x_search_query``). ``exclude_handles`` are accounts to leave out — the
    ones the user already follows plus any already on the current/next page —
    matched case-insensitively on the bare handle. Every returned account has
    at least one post from the last ``days`` days.

    Any failure (no key, bad JSON, empty reply, network error) returns ``[]`` —
    discovery must never raise.
    """
    if not query.strip():
        return []
    api_key = api_key if api_key is not None else settings().xai_api_key
    if not api_key:
        logger.info("x-discover: no XAI_API_KEY configured; skipping")
        return []

    complete = complete or _default_complete
    now = now or datetime.now(UTC)
    model = model or settings().xai_model
    cutoff = now - timedelta(days=days)
    excluded = {_normalize_handle(h) for h in exclude_handles}

    exclude_clause = ""
    if excluded:
        # Steer the model away from already-followed/already-shown accounts;
        # we also enforce this on parse, but asking up front saves wasted slots.
        handles = ", ".join("@" + h for h in sorted(excluded))
        exclude_clause = f"- Do NOT include any of these accounts: {handles}.\n"

    x_search_tool = {"type": "x_search", "from_date": cutoff.strftime("%Y-%m-%d")}
    payload = {
        "model": model,
        "max_output_tokens": _MAX_OUTPUT_TOKENS,
        "input": [
            {
                "role": "user",
                "content": _PROMPT.format(
                    max_results=max_results,
                    days=days,
                    min_posts=1,
                    max_posts=_MAX_POSTS_PER_ACCOUNT,
                    exclude_clause=exclude_clause,
                    query=query.strip(),
                ),
            }
        ],
        "tools": [x_search_tool],
    }

    try:
        response = complete(payload, api_key)
    except Exception as exc:  # noqa: BLE001 - best effort; never raise
        logger.warning("x-discover: API call failed: %s", exc)
        return []

    text = _output_text(response)
    if text is None:
        logger.warning("x-discover: no output text in response")
        return []

    json_str = extract_json_object(text)
    if json_str is None:
        logger.warning("x-discover: no JSON object in response: %.200s", text)
        return []

    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError as err:
        logger.warning("x-discover: could not parse response: %s", err)
        return []

    raw_accounts = parsed.get("accounts", []) if isinstance(parsed, dict) else []

    out: list[XAccount] = []
    seen: set[str] = set(excluded)
    for item in raw_accounts:
        account = _account_from(item, cutoff=cutoff)
        if account is None or account.handle in seen:
            continue
        seen.add(account.handle)
        out.append(account)
        if len(out) >= max_results:
            break
    return out
