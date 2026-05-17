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


def render_email(issue: Issue, public_base_url: str) -> tuple[str, str]:
    """Return ``(subject, html)`` for one issue."""
    subject = f"newslet — {issue.date}"
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

    html = _ENV.get_template("email.html.j2").render(
        date=issue.date, picks=ctx_picks
    )
    return subject, html
