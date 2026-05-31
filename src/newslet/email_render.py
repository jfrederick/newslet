"""Render a persisted :class:`Issue` into ``(subject, html)`` for sending."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, select_autoescape

from newslet import tokens
from newslet.contracts import Issue

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_ENV = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "j2"]),
)


def _display_date(date: str) -> str:
    """Human-facing date label for the header/subject.

    Manual "send now" runs store a synthetic key like
    ``manual-20260531-042944-7c43c81f`` (see ``digest._run_manual``); show
    just the calendar date (``2026-05-31``) rather than leaking that
    internal key into the email. Daily issues already store a clean
    ``YYYY-MM-DD`` and pass through unchanged.
    """
    if date.startswith("manual-"):
        parts = date.split("-")
        if len(parts) >= 2 and len(parts[1]) == 8 and parts[1].isdigit():
            stamp = parts[1]
            return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
    return date


def render_email(issue: Issue, public_base_url: str) -> tuple[str, str]:
    """Return ``(subject, html)`` for one issue."""
    display_date = _display_date(issue.date)
    subject = issue.subject or f"newslet — {display_date}"
    base = public_base_url.rstrip("/")
    sorted_picks = sorted(issue.picks, key=lambda p: p.score, reverse=True)

    ctx_picks: list[dict[str, str]] = []
    for pick in sorted_picks:
        url_str = str(pick.url)
        token = tokens.sign(url_str, issue.date)
        encoded = quote(url_str, safe="")
        common = f"a={encoded}&d={issue.date}"
        ctx_picks.append(
            {
                "url": url_str,
                "title": pick.title,
                "blurb": pick.blurb,
                "source": pick.source,
                "up_link": f"{base}/rate?{common}&v=up&t={token}",
                "down_link": f"{base}/rate?{common}&v=down&t={token}",
            }
        )

    ctx_discoveries = [
        {
            "url": str(d.url),
            "title": d.title,
            "source": d.source,
            "reason": d.reason,
        }
        for d in issue.discoveries
    ]

    html = _ENV.get_template("email.html.j2").render(
        date=display_date,
        picks=ctx_picks,
        intro=issue.intro,
        discoveries=ctx_discoveries,
        admin_url=f"{base}/",
    )
    return subject, html
