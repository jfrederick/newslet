"""Shared data shapes used across modules.

Every module (feeds, rank, email_render, db, handlers) imports from here
so the interfaces don't drift.  Pydantic models are used for JSON
boundaries (Claude responses, API requests); plain dataclasses for
internal value objects.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl

Rating = Literal["up", "down"]


class Article(BaseModel):
    """A candidate article fetched from RSS, before ranking."""

    url: HttpUrl
    title: str
    summary: str = ""
    source: str = Field(default="", description="Feed title or URL")
    published: datetime


class Pick(BaseModel):
    """One ranked article selected by Claude, ready for the email."""

    url: HttpUrl
    title: str
    blurb: str = Field(description="Claude-written one-line synopsis")
    source: str = ""
    score: float = Field(ge=0.0, le=1.0, default=0.5)


class RankResponse(BaseModel):
    """Top-level JSON shape Claude must return."""

    picks: list[Pick]


class WebArticle(BaseModel):
    """An article surfaced for the rich web view, not the email.

    Used for the "from around the web" block (Claude web-search results)
    and the on-demand subject search ("textbook"). Unlike :class:`Pick`,
    it carries optional engagement signal (``points``/``comments``) and a
    separate ``comments_url`` so Hacker News items can link to their
    discussion thread alongside the article itself. Lenient by design: the
    extra fields default to empty so a web-search result with no engagement
    data still validates.
    """

    url: HttpUrl
    title: str
    blurb: str = ""
    source: str = ""
    points: int | None = None
    comments: int | None = None
    comments_url: str = ""


class Discovery(BaseModel):
    """A new candidate source/article surfaced outside the user's feeds.

    ``feed_url`` is the RSS/Atom feed for the article's source, so the
    email can offer a one-click "subscribe" that adds it to the user's
    feeds. Discovery drops any result without one, so it is required here.
    """

    url: HttpUrl
    title: str
    source: str = ""
    reason: str = Field(default="", description="One line on why it is relevant")
    feed_url: HttpUrl = Field(description="RSS/Atom feed for the source")


class Issue(BaseModel):
    """A rendered daily issue persisted in DynamoDB."""

    date: str  # YYYY-MM-DD
    picks: list[Pick]
    created_at: datetime
    subject: str = ""
    intro: str = ""
    # The theme and text size the issue was (or will be) sent with, captured
    # at build time so the /emails/{date} archive keeps showing the email as
    # it was sent even after the admin changes appearance settings. The
    # defaults are what every pre-existing issue actually shipped with
    # (classic at 100%), NOT the app's current defaults — historical
    # accuracy, not preference.
    theme: str = "classic"
    text_size: int = 100
    discoveries: list[Discovery] = Field(default_factory=list)
    # The richer web view shows these in a "from around the web" block in
    # addition to ``picks``; the email never renders them. Optional with a
    # default so issues persisted before this field existed still load.
    web_articles: list[WebArticle] = Field(default_factory=list)
    # The "off your beat" block: popular past-week pieces deliberately outside
    # the reader's usual (tech) beat — see ``newslet.serendipity``. Rendered
    # on both the homepage and the email. Optional with a default so issues
    # persisted before this field existed still load.
    random_articles: list[WebArticle] = Field(default_factory=list)


class FeedbackRow(BaseModel):
    """One +/- click recorded from an email."""

    article_url: HttpUrl
    title: str
    rating: Rating
    ts: datetime
    issue_date: str  # YYYY-MM-DD; together with article_url, the table PK
    note: str = ""


class Feed(BaseModel):
    """An RSS feed the user has added."""

    url: HttpUrl
    title: str = ""
    added_at: datetime


class Subscription(BaseModel):
    """A newsletter subscription: a generated inbound address bound to a label.

    Each subscription owns a unique, ugly-but-working email address (e.g.
    ``n-a8f3c2d1@inbox.example.com``) the user pastes into a newsletter's
    signup form. Mail SES receives at that address is attributed to this
    subscription's ``source``. ``status`` tracks the double opt-in handshake:
    newly created subscriptions are ``pending`` until a confirmation email
    arrives (and is auto-confirmed), at which point they flip to ``confirmed``.
    """

    address: str  # full generated email address; the table PK
    source: str = Field(default="", description="User-facing label for the newsletter")
    status: Literal["pending", "confirmed"] = "pending"
    created_at: datetime
    confirmed_at: datetime | None = None
    last_received_at: datetime | None = None


class Profile(BaseModel):
    """The user's free-text profile (markdown) used in the rank prompt."""

    markdown: str
    updated_at: datetime


class Config(BaseModel):
    """Admin-tunable knobs for the daily email and web search.

    - ``max_rss_articles`` — how many ranked picks (RSS + Hacker News) the
      daily email carries.
    - ``max_web_articles`` — how many open-web search results the daily email
      carries (0 disables the web block in the email).
    - ``web_variety`` — 0–100 exploration dial for web search: 0 stays tightly
      on the user's stated interests, 100 ventures into related, ancillary
      areas (exploratory but never random/off-topic).
    - ``max_random_articles`` — how many "off your beat" articles (popular
      past-week pieces deliberately outside the reader's usual tech beat —
      see ``newslet.serendipity``) both surfaces carry (0 disables the block).
    - ``x_enabled`` — whether the X (Twitter) source runs. It also requires an
      ``XAI_API_KEY`` to be configured; turning this off disables X regardless
      of the key, so the user can pause the paid source without removing it.
    - ``max_x_articles`` — how many X posts to pull into the ranking pool when
      the source is enabled.
    - ``theme`` — visual theme for the web pages and the daily email. A plain
      string (not a Literal) so this module stays decoupled from
      ``newslet.themes``; consumers resolve it via ``themes.get``, which falls
      back to the default on unknown names, and the admin endpoint validates
      membership on write. The default mirrors ``themes.DEFAULT_THEME``
      (foundry).
    - ``text_size`` — percent of the browser's default text size, applied to
      the web pages (root ``font-size``; type is declared in ``rem``) and the
      daily email (scaled inline ``px``). Bounds mirror
      ``themes.TEXT_SIZE_MIN``/``MAX``.
    """

    max_rss_articles: int = Field(default=10, ge=1, le=40)
    max_web_articles: int = Field(default=5, ge=0, le=30)
    web_variety: int = Field(default=30, ge=0, le=100)
    max_random_articles: int = Field(default=4, ge=0, le=20)
    x_enabled: bool = Field(default=True)
    max_x_articles: int = Field(default=15, ge=1, le=30)
    theme: str = Field(default="foundry")
    text_size: int = Field(default=100, ge=75, le=150)
