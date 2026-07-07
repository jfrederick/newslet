"""Render a persisted :class:`Issue` into ``(subject, html)`` for sending."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, select_autoescape

from newslet import themes, tokens
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


def render_email(
    issue: Issue,
    public_base_url: str,
    theme: themes.Theme | None = None,
    text_size: int = 100,
) -> tuple[str, str]:
    """Return ``(subject, html)`` for one issue.

    ``theme`` styles the email via inline-style tokens (email clients ignore
    stylesheet classes); ``None`` renders the app default. ``text_size``
    (percent) scales every inline ``font-size`` — the email analogue of the
    web pages' root font-size dial.
    """
    theme = theme or themes.get(None)
    text_size = min(
        max(int(text_size), themes.TEXT_SIZE_MIN), themes.TEXT_SIZE_MAX
    )

    def fs(base_px: int) -> str:
        """Scale a design-time px size by the text-size dial."""
        return f"{round(base_px * text_size / 100)}px"

    display_date = _display_date(issue.date)
    subject = issue.subject or f"daily scoop — {display_date}"
    base = public_base_url.rstrip("/")
    sorted_picks = sorted(issue.picks, key=lambda p: p.score, reverse=True)

    def _rate_links(url_str: str) -> tuple[str, str]:
        """Signed +/- /rate links for an article (works from any inbox)."""
        token = tokens.sign(url_str, issue.date)
        common = f"a={quote(url_str, safe='')}&d={issue.date}"
        return (
            f"{base}/rate?{common}&v=up&t={token}",
            f"{base}/rate?{common}&v=down&t={token}",
        )

    ctx_picks: list[dict[str, str]] = []
    for pick in sorted_picks:
        url_str = str(pick.url)
        up_link, down_link = _rate_links(url_str)
        ctx_picks.append(
            {
                "url": url_str,
                "title": pick.title,
                "blurb": pick.blurb,
                "source": pick.source,
                "up_link": up_link,
                "down_link": down_link,
            }
        )

    # The "from around the web" block: votable just like picks (same signed
    # /rate mechanism) so feedback from the email still tunes ranking.
    ctx_web = []
    for w in issue.web_articles:
        url_str = str(w.url)
        up_link, down_link = _rate_links(url_str)
        ctx_web.append(
            {
                "url": url_str,
                "title": w.title,
                "blurb": w.blurb,
                "source": w.source,
                "points": w.points,
                "comments": w.comments,
                "comments_url": w.comments_url,
                "up_link": up_link,
                "down_link": down_link,
            }
        )

    # The "off your beat" block: same signed voting as picks/web, so feedback
    # on the deliberately-off-profile picks still lands in the ranking loop.
    ctx_random = []
    for r in issue.random_articles:
        url_str = str(r.url)
        up_link, down_link = _rate_links(url_str)
        ctx_random.append(
            {
                "url": url_str,
                "title": r.title,
                "blurb": r.blurb,
                "source": r.source,
                "up_link": up_link,
                "down_link": down_link,
            }
        )

    ctx_discoveries = []
    for d in issue.discoveries:
        feed_str = str(d.feed_url)
        # Sign over (feed_url, issue.date) like the rate links, so one click
        # from any email client adds the feed with no admin cookie, and the
        # issue date bounds replay scope.
        sub_token = tokens.sign(feed_str, issue.date)
        sub_common = f"f={quote(feed_str, safe='')}&d={issue.date}"
        if d.source:
            sub_common += f"&s={quote(d.source, safe='')}"
        ctx_discoveries.append(
            {
                "url": str(d.url),
                "title": d.title,
                "source": d.source,
                "reason": d.reason,
                "subscribe_link": f"{base}/subscribe?{sub_common}&t={sub_token}",
            }
        )

    html = _ENV.get_template("email.html.j2").render(
        t=theme,
        p=theme.palette,
        fs=fs,
        date=display_date,
        picks=ctx_picks,
        web_articles=ctx_web,
        random_articles=ctx_random,
        intro=issue.intro,
        discoveries=ctx_discoveries,
        # Generic link to the newslet homepage (the rich, browse-everything
        # web experience) — not this issue's page.
        home_url=f"{base}/",
    )
    return subject, html
