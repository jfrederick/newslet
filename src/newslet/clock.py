"""The app's calendar-day boundary.

The homepage's "is this today's edition?" test and its dateline are
anchored to the reader's local calendar day — US Eastern — not UTC.
Anchoring to UTC made every evening visit look stale: past 00:00 UTC
(~19:00–20:00 ET) the newest daily build was stamped "yesterday" even
though it was that morning's edition.

This module is the single owner of that boundary; nothing else should
hardcode a timezone.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")


def local_date(dt: datetime) -> date:
    """The Eastern calendar date of ``dt``.

    Naive datetimes are treated as UTC — that is what the persistence
    layer stores (``datetime.now(UTC)`` serialized via isoformat), so a
    legacy row that lost its offset still lands on the right day.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(EASTERN).date()


def local_now(now: datetime | None = None) -> datetime:
    """The current moment as an Eastern-zone datetime."""
    return (now or datetime.now(UTC)).astimezone(EASTERN)
