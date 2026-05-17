"""HMAC-based signing for rate links in newsletter emails.

Tokens authenticate ``(article_url, issue_date)`` pairs. They do not expire
on their own; replay scope is bounded by ``issue_date``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from newslet.config import settings


def _message(article_url: str, issue_date: str) -> bytes:
    return f"{article_url}|{issue_date}".encode()


def sign(article_url: str, issue_date: str) -> str:
    """Return a URL-safe base64 HMAC-SHA256 token (padding stripped)."""
    key = settings().signing_key.encode()
    digest = hmac.new(key, _message(article_url, issue_date), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify(article_url: str, issue_date: str, token: str) -> bool:
    """Constant-time verification. Returns ``False`` on any failure."""
    try:
        key = settings().signing_key.encode()
        expected = hmac.new(
            key, _message(article_url, issue_date), hashlib.sha256
        ).digest()
        # Re-pad the base64 token before decoding.
        padding = "=" * (-len(token) % 4)
        provided = base64.urlsafe_b64decode(token + padding)
        return hmac.compare_digest(expected, provided)
    except Exception:
        return False
