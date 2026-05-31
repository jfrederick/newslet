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
    discoveries: list[Discovery] = Field(default_factory=list)


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


class Profile(BaseModel):
    """The user's free-text profile (markdown) used in the rank prompt."""

    markdown: str
    updated_at: datetime
