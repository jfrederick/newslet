"""Inbound newsletter email handler (SES -> S3 -> this Lambda).

SES receives mail on the configured ``MAIL_DOMAIN``, drops the raw MIME into
the inbox S3 bucket, and invokes this Lambda with an SES event. We load the
raw message, match its recipient to a stored :class:`Subscription`, and then:

  * **confirmation request** — auto-follow the "click to confirm" link and
    flip the subscription to ``confirmed`` (double opt-in, hands-free);
  * **regular newsletter** — extract the article links and store them as
    candidates the daily digest folds into its ranking pool.

Best-effort and self-contained: any single message failure is logged and
swallowed so SES doesn't retry-storm the function. The network edges (S3 read,
confirmation-link follow) are injectable so tests stay offline.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from newslet import db, newsletters
from newslet.config import settings

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# SES stores objects at ``<prefix><messageId>``; keep this in lockstep with the
# ObjectKeyPrefix on the receipt rule's S3 action in infra/template.yaml.
_S3_PREFIX = "inbound/"

# A few seconds is plenty for a confirmation GET; we don't want a slow target
# to wedge the Lambda.
_CONFIRM_TIMEOUT = 10
_CONFIRM_UA = "Mozilla/5.0 (compatible; newslet-newsletter-confirm/1.0)"


def _load_raw(message_id: str) -> bytes:
    """Fetch the raw MIME SES stored in S3 for this message."""
    import boto3

    s3 = boto3.client("s3")
    key = f"{_S3_PREFIX}{message_id}"
    obj = s3.get_object(Bucket=settings().inbox_bucket, Key=key)
    return obj["Body"].read()


def _is_public_url(url: str) -> bool:
    """True if ``url`` is http(s) and every resolved IP is publicly routable.

    Confirmation links come from untrusted inbound email, so before we GET one
    we refuse anything that resolves into private/loopback/link-local space —
    otherwise a confirmation-shaped email could drive an arbitrary internal GET
    (SSRF). Re-checked on every redirect hop via :class:`_PublicOnlyRedirect`.
    """
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port)
    except OSError:
        return False
    for *_, sockaddr in infos:
        try:
            if not ipaddress.ip_address(sockaddr[0]).is_global:
                return False
        except ValueError:
            return False
    return True


class _PublicOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Re-validate each redirect target so a 3xx can't bounce us into a private host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _is_public_url(newurl):
            raise urllib.error.URLError(f"unsafe redirect target: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _follow_link(url: str) -> bool:
    """GET a confirmation URL; True on a 2xx/3xx response, False otherwise.

    Newsletters confirm via a plain link click, so a GET is almost always the
    right verb. We never raise — a failed (or refused) confirm just leaves the
    subscription ``pending`` for the user to resolve manually.
    """
    if not _is_public_url(url):
        log.warning("refusing to follow non-public confirmation link %s", url)
        return False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _CONFIRM_UA})
        opener = urllib.request.build_opener(_PublicOnlyRedirect)
        with opener.open(req, timeout=_CONFIRM_TIMEOUT) as resp:  # noqa: S310
            return 200 <= resp.status < 400
    except Exception:  # noqa: BLE001 - any network/HTTP error -> not confirmed
        log.warning("confirmation follow failed for %s", url, exc_info=True)
        return False


def _match_subscription(recipients: list[str], parsed: newsletters.ParsedEmail):
    """Find the subscription a message was sent to.

    Checks the SES-supplied recipients first (authoritative), then the
    addresses parsed from the message headers as a fallback.
    """
    candidates: list[str] = []
    candidates.extend(r.lower() for r in recipients if r)
    candidates.extend(parsed.to_addrs)
    for addr in dict.fromkeys(candidates):
        sub = db.get_subscription(addr)
        if sub is not None:
            return sub
    return None


def process_message(
    raw: bytes,
    recipients: list[str],
    message_id: str,
    *,
    confirm: Callable[[str], bool] = _follow_link,
    now: datetime | None = None,
) -> dict:
    """Core, network-injectable processing for a single inbound message."""
    now = now or datetime.now(UTC)
    parsed = newsletters.parse_email(raw)
    sub = _match_subscription(recipients, parsed)
    if sub is None:
        log.warning("no subscription matches recipients %r; dropping", recipients)
        return {"status": "no_match"}

    # Only treat a message as a confirmation when the subscription is still
    # awaiting opt-in AND we actually find a confirmation link. A confirmation
    # false-positive (footer boilerplate on a real issue) then falls through to
    # extraction rather than silently dropping that issue's articles.
    if sub.status == "pending" and newsletters.is_confirmation(parsed):
        link = newsletters.find_confirmation_link(parsed)
        if link:
            ok = confirm(link)
            if ok:
                db.mark_subscription_confirmed(sub.address, when=now)
                log.info("auto-confirmed subscription %s (%s)", sub.address, sub.source)
            return {
                "status": "confirmed" if ok else "confirm_failed",
                "address": sub.address,
            }
        log.info(
            "confirmation-shaped email for %s but no link; extracting articles",
            sub.address,
        )

    articles = newsletters.extract_articles(parsed, source=sub.source, now=now)
    db.put_inbox_email(
        message_id=message_id,
        source=sub.source,
        address=sub.address,
        articles=articles,
        received_at=now,
    )
    db.touch_subscription(sub.address, when=now)
    log.info(
        "stored %d articles from %s (%s)", len(articles), sub.address, sub.source
    )
    return {"status": "stored", "address": sub.address, "articles": len(articles)}


def _process_record(ses: dict, *, loader: Callable[[str], bytes] | None = None) -> dict:
    # Resolve _load_raw at call time (not as a default) so tests can
    # monkeypatch the module attribute to stay off S3.
    loader = loader or _load_raw
    mail = ses.get("mail", {}) or {}
    receipt = ses.get("receipt", {}) or {}
    message_id = mail.get("messageId") or ""
    recipients = receipt.get("recipients") or mail.get("destination") or []
    raw = loader(message_id)
    return process_message(raw, recipients, message_id)


def handler(event: dict, context: Any) -> dict:
    """SES-invoked entry point. Processes every record, never raising.

    A raised exception would make SES retry the message, so each record is
    isolated: a failure is logged and the rest still run.
    """
    results: list[dict] = []
    for record in event.get("Records", []):
        ses = record.get("ses", {})
        try:
            results.append(_process_record(ses))
        except Exception:  # noqa: BLE001 - one bad message must not fail the batch
            log.exception("failed to process inbound record; skipping")
            results.append({"status": "error"})
    return {"processed": len(results), "results": results}
