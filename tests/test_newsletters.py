"""Tests for :mod:`newslet.newsletters` — email parsing + link extraction."""

from __future__ import annotations

from datetime import UTC, datetime
from email.message import EmailMessage

import pytest

from newslet import newsletters


def _build_email(
    *,
    sender: str = "The Newsletter <hi@news.example.com>",
    to: str = "n-abc123@inbox.example.com",
    subject: str = "Today's stories",
    html: str | None = None,
    text: str | None = None,
    date: str | None = "Tue, 02 Jun 2026 09:00:00 +0000",
) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    if date:
        msg["Date"] = date
    if text is not None:
        msg.set_content(text)
    if html is not None:
        if text is not None:
            msg.add_alternative(html, subtype="html")
        else:
            msg.set_content(html, subtype="html")
    return msg.as_bytes()


# ---------------------------------------------------------------------------
# generate_address
# ---------------------------------------------------------------------------


def test_generate_address_under_domain():
    addr = newsletters.generate_address("inbox.example.com")
    assert addr.endswith("@inbox.example.com")
    assert addr.startswith("n-")
    # Two calls don't collide.
    assert newsletters.generate_address("inbox.example.com") != addr


def test_generate_address_normalizes_and_requires_domain():
    assert newsletters.generate_address(" @Inbox.Example.COM ").endswith(
        "@inbox.example.com"
    )
    with pytest.raises(ValueError):
        newsletters.generate_address("")


# ---------------------------------------------------------------------------
# parse_email
# ---------------------------------------------------------------------------


def test_parse_email_extracts_headers_and_bodies():
    raw = _build_email(
        html="<p>Hello <a href='https://x.example/a'>a link here yes</a></p>",
        text="Hello plain",
    )
    parsed = newsletters.parse_email(raw)
    assert parsed.from_addr == "hi@news.example.com"
    assert parsed.from_name == "The Newsletter"
    assert "n-abc123@inbox.example.com" in parsed.to_addrs
    assert parsed.subject == "Today's stories"
    assert "a link here yes" in parsed.html
    assert "Hello plain" in parsed.text
    assert parsed.date is not None
    assert parsed.date.year == 2026


def test_parse_email_never_raises_on_garbage():
    parsed = newsletters.parse_email(b"this is not a real mime message")
    assert isinstance(parsed, newsletters.ParsedEmail)


# ---------------------------------------------------------------------------
# extract_articles
# ---------------------------------------------------------------------------


def test_extract_articles_keeps_headlines_drops_boilerplate():
    html = """
      <h1>The Daily</h1>
      <a href="https://site.example/story-one">A genuinely interesting headline today</a>
      <a href="https://site.example/story-two">Another substantial story worth reading</a>
      <a href="https://site.example/unsubscribe">Unsubscribe</a>
      <a href="https://twitter.com/foo">Follow us on Twitter</a>
      <a href="https://site.example/x">Read more</a>
      <a href="mailto:hi@site.example">email us</a>
    """
    parsed = newsletters.parse_email(_build_email(html=html))
    arts = newsletters.extract_articles(parsed, source="The Daily")
    urls = [str(a.url) for a in arts]
    assert "https://site.example/story-one" in urls
    assert "https://site.example/story-two" in urls
    # Boilerplate / social / CTA / mailto all dropped.
    assert not any("unsubscribe" in u for u in urls)
    assert not any("twitter.com" in u for u in urls)
    assert not any(u.endswith("/x") for u in urls)
    # Title comes from the anchor text; source from the label.
    assert arts[0].title == "A genuinely interesting headline today"
    assert arts[0].source == "The Daily"


def test_extract_articles_dedupes_and_uses_email_date():
    html = """
      <a href="https://site.example/dup">A headline that appears twice here</a>
      <a href="https://site.example/dup">A headline that appears twice here</a>
    """
    parsed = newsletters.parse_email(
        _build_email(html=html, date="Tue, 02 Jun 2026 09:00:00 +0000")
    )
    arts = newsletters.extract_articles(parsed, source="Src")
    assert len(arts) == 1
    assert arts[0].published.year == 2026
    assert arts[0].published.month == 6


def test_extract_articles_falls_back_to_now_without_date():
    html = '<a href="https://s.example/p">A long enough headline here</a>'
    parsed = newsletters.parse_email(_build_email(html=html, date=None))
    fixed = datetime(2026, 1, 1, tzinfo=UTC)
    arts = newsletters.extract_articles(parsed, source="S", now=fixed)
    assert arts[0].published == fixed


def test_extract_articles_plain_text_fallback():
    parsed = newsletters.parse_email(
        _build_email(html=None, text="See https://s.example/story and more")
    )
    arts = newsletters.extract_articles(parsed, source="S")
    assert [str(a.url) for a in arts] == ["https://s.example/story"]


# ---------------------------------------------------------------------------
# confirmation detection
# ---------------------------------------------------------------------------


def test_is_confirmation_on_subject():
    parsed = newsletters.parse_email(
        _build_email(subject="Please confirm your subscription", html="<p>hi</p>")
    )
    assert newsletters.is_confirmation(parsed) is True


def test_is_confirmation_on_body():
    parsed = newsletters.parse_email(
        _build_email(
            subject="Hello",
            html="<p>Click the link below to confirm your subscription.</p>",
        )
    )
    assert newsletters.is_confirmation(parsed) is True


def test_regular_newsletter_is_not_confirmation():
    parsed = newsletters.parse_email(
        _build_email(
            subject="This week in tech",
            html='<a href="https://s.example/p">A normal story headline here</a>',
        )
    )
    assert newsletters.is_confirmation(parsed) is False


def test_find_confirmation_link_prefers_anchor_text():
    html = """
      <a href="https://news.example/home">Visit our homepage</a>
      <a href="https://news.example/c/abc123">Confirm your subscription</a>
      <a href="https://news.example/unsubscribe">unsubscribe</a>
    """
    parsed = newsletters.parse_email(_build_email(subject="Confirm", html=html))
    assert newsletters.find_confirmation_link(parsed) == "https://news.example/c/abc123"


def test_find_confirmation_link_falls_back_to_url_token():
    html = """
      <a href="https://news.example/home">click the button</a>
      <a href="https://news.example/verify/xyz">tap to continue</a>
    """
    parsed = newsletters.parse_email(_build_email(subject="Verify", html=html))
    assert newsletters.find_confirmation_link(parsed) == "https://news.example/verify/xyz"


def test_find_confirmation_link_none_when_absent():
    parsed = newsletters.parse_email(
        _build_email(subject="Confirm", html='<a href="https://news.example/home">home</a>')
    )
    assert newsletters.find_confirmation_link(parsed) is None


def test_find_confirmation_link_allows_account_path():
    # An /account-style confirm link must not be pre-filtered as boilerplate.
    html = '<a href="https://account.example.com/verify?token=abc">Confirm your subscription</a>'
    parsed = newsletters.parse_email(_build_email(subject="Confirm", html=html))
    assert (
        newsletters.find_confirmation_link(parsed)
        == "https://account.example.com/verify?token=abc"
    )


def test_find_confirmation_link_ignores_click_tracker():
    # A generic /c/ click-tracker is not a confirmation link.
    html = '<a href="https://news.example/c/track123">Read the full story</a>'
    parsed = newsletters.parse_email(_build_email(subject="Confirm", html=html))
    assert newsletters.find_confirmation_link(parsed) is None
