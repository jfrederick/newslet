"""Tests for :mod:`newslet.email_render`."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from urllib.parse import quote, unquote

import pytest

from newslet import tokens
from newslet.config import settings
from newslet.contracts import Discovery, Issue, Pick
from newslet.email_render import render_email

BASE_URL = "https://api.example.test"
DATE = "2026-05-17"


def _pick(url: str, title: str, blurb: str, score: float = 0.5) -> Pick:
    return Pick(url=url, title=title, blurb=blurb, source="src", score=score)


def _issue(
    picks: list[Pick],
    *,
    subject: str = "",
    intro: str = "",
    discoveries: list[Discovery] | None = None,
) -> Issue:
    return Issue(
        date=DATE,
        picks=picks,
        created_at=datetime(2026, 5, 17, tzinfo=UTC),
        subject=subject,
        intro=intro,
        discoveries=discoveries or [],
    )


@pytest.fixture
def stub_sign(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tokens, "sign", lambda url, date: "STUBTOKEN")


@pytest.fixture
def real_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-anthropic")
    monkeypatch.setenv("RESEND_API_KEY", "dummy-resend")
    monkeypatch.setenv("FROM_EMAIL", "from@example.com")
    monkeypatch.setenv("TO_EMAIL", "to@example.com")
    monkeypatch.setenv("ADMIN_TOKEN", "dummy-admin")
    monkeypatch.setenv("SIGNING_KEY", "dummy-signing-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)
    settings.cache_clear()
    yield
    settings.cache_clear()


def test_smoke(stub_sign: None) -> None:
    issue = _issue(
        [
            _pick("https://a.example.com/1", "Alpha title", "Alpha blurb"),
            _pick("https://b.example.com/2", "Beta title", "Beta blurb"),
        ]
    )
    subject, html = render_email(issue, BASE_URL)
    assert subject == "newslet — 2026-05-17"
    assert html.lstrip().startswith("<")
    assert "Alpha title" in html
    assert "Beta title" in html
    assert "Alpha blurb" in html
    assert "Beta blurb" in html


def test_sort_order(stub_sign: None) -> None:
    issue = _issue(
        [
            _pick("https://a.example.com/low", "LowTitle", "lo", score=0.3),
            _pick("https://a.example.com/high", "HighTitle", "hi", score=0.9),
            _pick("https://a.example.com/mid", "MidTitle", "mi", score=0.5),
        ]
    )
    _, html = render_email(issue, BASE_URL)
    hi = html.index("HighTitle")
    mi = html.index("MidTitle")
    lo = html.index("LowTitle")
    assert hi < mi < lo


def test_rate_links_well_formed(stub_sign: None) -> None:
    urls = ["https://a.example.com/one?x=1", "https://b.example.com/two"]
    issue = _issue(
        [
            _pick(urls[0], "T1", "B1", score=0.9),
            _pick(urls[1], "T2", "B2", score=0.1),
        ]
    )
    _, html = render_email(issue, BASE_URL)
    # Two up + two down links, each carrying a token param.
    assert html.count("v=up") == 2
    assert html.count("v=down") == 2
    assert html.count("t=STUBTOKEN") == 4
    for u in urls:
        encoded = quote(u, safe="")
        # Jinja autoescape turns & into &amp;
        assert f"a={encoded}".replace("&", "&amp;") in html or f"a={encoded}" in html


def test_token_verifies(real_env: None) -> None:
    url = "https://example.com/article/42"
    issue = _issue([_pick(url, "Title", "Blurb", score=0.7)])
    _, html = render_email(issue, BASE_URL)

    # Extract one rate link href.
    m = re.search(r'href="([^"]*\bv=up[^"]*)"', html)
    assert m is not None
    href = m.group(1).replace("&amp;", "&")

    a_match = re.search(r"[?&]a=([^&]+)", href)
    t_match = re.search(r"[?&]t=([^&]+)", href)
    assert a_match and t_match
    decoded_a = unquote(a_match.group(1))
    token = t_match.group(1)
    assert tokens.verify(decoded_a, issue.date, token) is True


def test_empty_picks(stub_sign: None) -> None:
    issue = _issue([])
    subject, html = render_email(issue, BASE_URL)
    assert subject == "newslet — 2026-05-17"
    assert "<!doctype html>" in html.lower() or html.lstrip().startswith("<")
    assert "0 picks today" in html


def test_trailing_slash_idempotent(stub_sign: None) -> None:
    issue = _issue([_pick("https://x.example.com/p", "T", "B")])
    _, html_slash = render_email(issue, "https://api/")
    _, html_no_slash = render_email(issue, "https://api")
    assert html_slash == html_no_slash
    assert "https://api//rate" not in html_slash


def test_subject_falls_back_when_empty(stub_sign: None) -> None:
    issue = _issue([_pick("https://a.example.com/1", "T", "B")])
    subject, _ = render_email(issue, BASE_URL)
    assert subject == "newslet — 2026-05-17"


def test_subject_override_used_when_present(stub_sign: None) -> None:
    issue = _issue(
        [_pick("https://a.example.com/1", "T", "B")],
        subject="The big thing today",
    )
    subject, _ = render_email(issue, BASE_URL)
    assert subject == "The big thing today"


def test_intro_renders_above_picks(stub_sign: None) -> None:
    issue = _issue(
        [_pick("https://a.example.com/1", "AlphaTitle", "B")],
        intro="Here is what is worth your time today.",
    )
    _, html = render_email(issue, BASE_URL)
    assert "Here is what is worth your time today." in html
    # Intro appears above the picks.
    assert html.index("Here is what is worth your time today.") < html.index("AlphaTitle")


def test_intro_omitted_when_empty(stub_sign: None) -> None:
    issue = _issue([_pick("https://a.example.com/1", "AlphaTitle", "B")])
    _, html = render_email(issue, BASE_URL)
    # No stray empty intro cell; picks still render.
    assert "AlphaTitle" in html


def test_discoveries_section_renders(stub_sign: None) -> None:
    discoveries = [
        Discovery(
            url="https://newsource.example.org/story",
            title="A Fresh Discovery",
            source="New Source Weekly",
            reason="It matches your interest in fresh things.",
        )
    ]
    issue = _issue(
        [_pick("https://a.example.com/1", "AlphaTitle", "B")],
        discoveries=discoveries,
    )
    _, html = render_email(issue, BASE_URL)
    assert "Sources you" in html and "follow yet" in html
    assert "A Fresh Discovery" in html
    assert "https://newsource.example.org/story" in html
    assert "New Source Weekly" in html
    assert "It matches your interest in fresh things." in html


def test_discoveries_section_omitted_when_empty(stub_sign: None) -> None:
    issue = _issue([_pick("https://a.example.com/1", "AlphaTitle", "B")])
    _, html = render_email(issue, BASE_URL)
    assert "follow yet" not in html


def test_admin_link_present(stub_sign: None) -> None:
    issue = _issue([_pick("https://a.example.com/1", "T", "B")])
    _, html = render_email(issue, BASE_URL)
    assert "Manage newslet" in html
    assert f'href="{BASE_URL}/"' in html


def test_admin_link_trailing_slash_idempotent(stub_sign: None) -> None:
    issue = _issue([_pick("https://a.example.com/1", "T", "B")])
    _, html_slash = render_email(issue, "https://api/")
    _, html_no_slash = render_email(issue, "https://api")
    assert html_slash == html_no_slash
    assert 'href="https://api/"' in html_no_slash
    assert "https://api//" not in html_no_slash
