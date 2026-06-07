"""Parse inbound newsletter emails into candidate articles.

This is the newsletter source's analogue of :mod:`newslet.feeds`: it turns a
raw inbound email into :class:`~newslet.contracts.Article` candidates the
ranker can see. Pure parsing + link extraction + double-opt-in detection; no
DynamoDB and no network (the confirmation-link *follow* lives in the inbound
handler, where the network edge is injectable for tests).

Newsletters vary wildly in markup, so extraction is deliberately heuristic and
lenient: we pull out the substantial article links (headline-shaped anchor
text), drop the boilerplate (unsubscribe / preferences / social / "view in
browser"), and never raise on a malformed message.
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlparse

from pydantic import ValidationError

from newslet.contracts import Article

log = logging.getLogger(__name__)

# Local-part length: 4 random bytes -> 8 hex chars. Ugly but plenty unique for
# a personal app, and short enough to type if a signup form rejects long names.
_ADDRESS_BYTES = 4


def generate_address(domain: str) -> str:
    """Mint a fresh, unique inbound address under ``domain``.

    Raises ``ValueError`` if no domain is configured yet — the caller (admin
    UI) turns that into a "configure MAIL_DOMAIN first" message rather than
    handing out a broken address.
    """
    domain = (domain or "").strip().lstrip("@").lower()
    if not domain:
        raise ValueError("no mail domain configured")
    return f"n-{secrets.token_hex(_ADDRESS_BYTES)}@{domain}"


# ---------------------------------------------------------------------------
# Parsed email value object
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ParsedEmail:
    """A decoded inbound message — the input to extraction/confirmation."""

    from_addr: str = ""
    from_name: str = ""
    to_addrs: list[str] = field(default_factory=list)
    subject: str = ""
    html: str = ""
    text: str = ""
    date: datetime | None = None


def _addresses(value: str) -> list[str]:
    """Best-effort split of a header value into bare lowercase addresses."""
    from email.utils import getaddresses

    return [addr.lower() for _name, addr in getaddresses([value]) if addr]


def parse_email(raw: bytes) -> ParsedEmail:
    """Decode raw MIME bytes into a :class:`ParsedEmail`. Never raises."""
    try:
        msg = message_from_bytes(raw, policy=policy.default)
    except Exception:  # noqa: BLE001 - a corrupt message must not crash the Lambda
        log.exception("failed to parse inbound MIME; treating as empty")
        return ParsedEmail()

    from email.utils import parseaddr

    from_name, from_addr = parseaddr(msg.get("From", ""))

    # Recipients can show up across several headers; SES also hands the
    # authoritative list to the handler, so this is a secondary signal.
    to_addrs: list[str] = []
    for header in ("To", "Cc", "Delivered-To", "X-Original-To", "X-Forwarded-To"):
        for value in msg.get_all(header, []):
            to_addrs.extend(_addresses(value))

    date: datetime | None = None
    if msg.get("Date"):
        try:
            date = parsedate_to_datetime(msg["Date"])
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            date = None

    return ParsedEmail(
        from_addr=(from_addr or "").lower(),
        from_name=from_name or "",
        to_addrs=list(dict.fromkeys(to_addrs)),
        subject=msg.get("Subject", "") or "",
        html=_body(msg, "html"),
        text=_body(msg, "plain"),
        date=date,
    )


def _body(msg, subtype: str) -> str:
    """Return the decoded text of the message's ``subtype`` part, or ""."""
    try:
        part = msg.get_body(preferencelist=(subtype,))
    except Exception:  # noqa: BLE001 - get_body can choke on odd structures
        part = None
    if part is None:
        return ""
    try:
        content = part.get_content()
    except Exception:  # noqa: BLE001 - bad charset / encoding
        return ""
    return content if isinstance(content, str) else ""


# ---------------------------------------------------------------------------
# Link extraction
# ---------------------------------------------------------------------------


class _LinkExtractor(HTMLParser):
    """Collect ``(href, anchor_text)`` pairs from an HTML body."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                # A nested <a> is invalid HTML; flush the outer one first.
                self._flush()
                self._href = href
                self._buf = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._flush()

    def _flush(self) -> None:
        if self._href is not None:
            self.links.append((self._href, " ".join("".join(self._buf).split())))
            self._href = None
            self._buf = []


# Hosts whose links are navigation/social chrome, never the article itself.
_SKIP_HOSTS = {
    "twitter.com", "www.twitter.com", "x.com", "www.x.com",
    "facebook.com", "www.facebook.com", "instagram.com", "www.instagram.com",
    "linkedin.com", "www.linkedin.com", "youtube.com", "www.youtube.com",
    "t.me", "threads.net", "mastodon.social", "bsky.app",
    "tiktok.com", "www.tiktok.com", "reddit.com", "www.reddit.com",
    "pinterest.com", "wa.me", "api.whatsapp.com",
}

# Path/query fragments that mark a link as boilerplate rather than content.
_SKIP_PATH = re.compile(
    r"unsubscribe|/unsub|manage[-_/]?(your[-_/]?)?(sub|email|preference)|"
    r"opt[-_]?out|email[-_]?(setting|preference)|view[-_]?(this|in)[-_]?(email|browser)|"
    r"web[-_]?version|/profile|/account|/privacy|/terms|/feed\b|list-manage",
    re.I,
)

# Narrower skip set for confirmation-link selection: only unsubscribe/manage
# links are excluded. Account/profile paths are kept because a confirm link
# often lives there (e.g. /account/verify), which _SKIP_PATH would discard.
_SKIP_CONFIRM_PATH = re.compile(
    r"unsubscribe|/unsub|manage[-_/]?(your[-_/]?)?(sub|email|preference)|"
    r"opt[-_]?out|email[-_]?(setting|preference)|list-manage",
    re.I,
)

# Anchor texts that are calls-to-action / chrome, not headlines.
_SKIP_ANCHOR = {
    "", "read more", "read", "read on", "continue reading", "view", "view more",
    "click here", "here", "more", "subscribe", "unsubscribe", "share", "tweet",
    "forward", "forward to a friend", "manage", "update preferences", "website",
    "home", "view in browser", "view online", "read in app", "open in app",
    "comment", "comments", "like", "reply", "follow", "view in your browser",
}

# Headline-shaped anchor text: long enough, or several words.
_MIN_TITLE_LEN = 18
_MIN_TITLE_WORDS = 4


def extract_links(parsed: ParsedEmail) -> list[tuple[str, str]]:
    """Return ``(url, anchor_text)`` pairs from the message body.

    Prefers the HTML part; falls back to bare URLs found in the plain-text
    part (with no usable anchor text) when there is no HTML.
    """
    if parsed.html:
        extractor = _LinkExtractor()
        try:
            extractor.feed(parsed.html)
            extractor._flush()
        except Exception:  # noqa: BLE001 - never let bad markup crash extraction
            log.warning("HTML parse error during link extraction", exc_info=True)
        return extractor.links

    # Plain-text fallback: grab http(s) URLs, no titles available.
    return [(m.group(0), "") for m in re.finditer(r"https?://[^\s<>()\"']+", parsed.text)]


def _is_article_url(url: str) -> bool:
    """True if the URL itself isn't obvious chrome (social/unsubscribe/etc.)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    if parsed.netloc.lower() in _SKIP_HOSTS:
        return False
    return not _SKIP_PATH.search(url)


def _is_headline_anchor(anchor: str) -> bool:
    """True if the anchor text reads like a story title, not a CTA/chrome."""
    text = anchor.strip()
    if text.lower() in _SKIP_ANCHOR:
        return False
    return len(text) >= _MIN_TITLE_LEN or len(text.split()) >= _MIN_TITLE_WORDS


def extract_articles(
    parsed: ParsedEmail,
    source: str,
    *,
    now: datetime | None = None,
    max_articles: int = 30,
) -> list[Article]:
    """Turn an inbound newsletter into ranked-pool article candidates.

    Each substantial link becomes one candidate; the anchor text is its title,
    the sender (or ``source`` label) is its source, and the message's Date (or
    ``now``) is its published time so the digest's 24h window includes it.
    """
    now = now or datetime.now(UTC)
    published = parsed.date or now
    label = source or parsed.from_name or parsed.from_addr or "Newsletter"

    # With HTML we have anchor text and can demand headline-shaped titles; a
    # plain-text-only newsletter is just a list of URLs, so keep those bare.
    has_html = bool(parsed.html)

    out: list[Article] = []
    seen: set[str] = set()
    for href, anchor in extract_links(parsed):
        url = href.strip()
        if not _is_article_url(url):
            continue
        if has_html and not _is_headline_anchor(anchor):
            continue
        if url in seen:
            continue
        seen.add(url)
        title = (anchor.strip() or url)[:300]
        try:
            out.append(
                Article(
                    url=url,
                    title=title,
                    summary="",
                    source=label,
                    published=published,
                )
            )
        except ValidationError:
            continue
        if len(out) >= max_articles:
            break
    return out


# ---------------------------------------------------------------------------
# Double opt-in confirmation
# ---------------------------------------------------------------------------


_CONFIRM_SUBJECT = re.compile(
    r"\bconfirm\b|\bverify\b|opt[-\s]?in|please\s+confirm|finish\s+(subscrib|sign)|"
    r"activate\s+your\s+(subscription|account|email)",
    re.I,
)
_CONFIRM_BODY = re.compile(
    r"confirm(ing)?\s+(your\s+)?(subscription|email|sign[\s-]?up|request)|"
    r"verify\s+(your\s+)?(email|address|subscription)|"
    r"activate\s+(your\s+)?(subscription|account)|"
    r"click\s+(the\s+link|below|here)\s+to\s+confirm|confirm\s+now",
    re.I,
)
# A link whose URL or anchor text screams "this confirms the subscription".
_CONFIRM_LINK = re.compile(
    r"confirm|verify|activate|opt[-_]?in|/confirm|subscription/confirm|"
    r"double[-_]?opt", re.I,
)
_CONFIRM_ANCHOR = re.compile(
    r"confirm|verify|activate|yes,?\s|subscribe me|complete", re.I
)


def _confirmation_body(parsed: ParsedEmail) -> str:
    """Plain text to keyword-match for confirmation intent.

    Prefer the text part; otherwise strip tags from the HTML so footer markup
    (``href``s, class names) can't read as confirmation phrasing.
    """
    if parsed.text:
        return parsed.text
    return re.sub(r"<[^>]+>", " ", parsed.html)


def is_confirmation(parsed: ParsedEmail) -> bool:
    """True if this message looks like a double-opt-in confirmation request."""
    if _CONFIRM_SUBJECT.search(parsed.subject):
        return True
    return bool(_CONFIRM_BODY.search(_confirmation_body(parsed)))


def find_confirmation_link(parsed: ParsedEmail) -> str | None:
    """Pick the most likely "click to confirm" link, or None.

    Prefers a link whose *anchor text* asks to confirm (the visible button),
    then falls back to one whose *URL* contains a confirm/verify token.
    """
    links = [
        (href.strip(), anchor)
        for href, anchor in extract_links(parsed)
        if urlparse(href.strip()).scheme in ("http", "https")
        and not _SKIP_CONFIRM_PATH.search(href)
        and urlparse(href.strip()).netloc.lower() not in _SKIP_HOSTS
    ]
    for href, anchor in links:
        if anchor and _CONFIRM_ANCHOR.search(anchor):
            return href
    for href, _anchor in links:
        if _CONFIRM_LINK.search(href):
            return href
    return None
