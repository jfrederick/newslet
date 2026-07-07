"""Tests for the Eastern day boundary (newslet.clock)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from newslet import clock


def test_local_date_converts_utc_to_eastern():
    # 01:00 UTC on July 7 is still 21:00 on July 6 in New York (EDT) — the
    # exact evening rollover that made the homepage look stale every night.
    dt = datetime(2026, 7, 7, 1, 0, tzinfo=UTC)
    assert dt.date() == date(2026, 7, 7)
    assert clock.local_date(dt) == date(2026, 7, 6)


def test_local_date_midday_matches_utc_date():
    dt = datetime(2026, 7, 7, 15, 0, tzinfo=UTC)  # 11:00 ET, same calendar day
    assert clock.local_date(dt) == date(2026, 7, 7)


def test_local_date_handles_est_winter_offset():
    # 04:30 UTC in January (EST, UTC-5) is 23:30 the previous day Eastern.
    dt = datetime(2026, 1, 15, 4, 30, tzinfo=UTC)
    assert clock.local_date(dt) == date(2026, 1, 14)


def test_local_date_treats_naive_as_utc():
    naive = datetime(2026, 7, 7, 1, 0)  # what a legacy row without offset yields
    aware = naive.replace(tzinfo=UTC)
    assert clock.local_date(naive) == clock.local_date(aware) == date(2026, 7, 6)


def test_local_now_is_eastern():
    now = datetime(2026, 7, 7, 1, 0, tzinfo=UTC)
    local = clock.local_now(now)
    assert local.tzinfo is clock.EASTERN
    assert local.date() == date(2026, 7, 6)
    assert local == now  # same instant, different wall clock
