"""One terse weather line from the National Weather Service API.

Free, keyless, and template-formatted — no LLM anywhere. Two requests:
``/points/{lat},{lon}`` resolves the forecast URL for the coordinates,
then the forecast's first two periods become a line like::

    78° chance light rain, tonight 64° mostly clear

The line is stamped on the Issue at build time so archive views keep the
weather the reader actually woke up to. Best-effort like every enrichment:
any network/shape problem yields ``None`` and the line is simply absent.

The default coordinates are Brooklyn, NY; callers can override per call.
(Deliberately not admin-configurable yet — a second city is YAGNI until it
isn't.)
"""

from __future__ import annotations

import json
import logging
import urllib.request

logger = logging.getLogger(__name__)

BROOKLYN_LAT = 40.678
BROOKLYN_LON = -73.944

# api.weather.gov requires a descriptive User-Agent and may throttle
# anonymous defaults.
_USER_AGENT = "newslet (https://github.com/jfrederick/newslet)"
_TIMEOUT_SECONDS = 10


def _default_fetch(url: str) -> dict:
    """GET ``url`` and JSON-decode it (the injectable network edge)."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310 - https API constant
        return json.loads(resp.read().decode("utf-8"))


def _format_period(period: dict, *, with_name: bool) -> str | None:
    """``"78° chance light rain"`` (optionally ``"tonight 64° …"``)."""
    temp = period.get("temperature")
    short = str(period.get("shortForecast", "")).strip().lower()
    if not isinstance(temp, int | float) or not short:
        return None
    text = f"{round(temp)}° {short}"
    if with_name:
        name = str(period.get("name", "")).strip().lower()
        if not name:
            return None
        text = f"{name} {text}"
    return text


def fetch_weather(
    *,
    lat: float = BROOKLYN_LAT,
    lon: float = BROOKLYN_LON,
    fetch=None,
) -> str | None:
    """Return the one-line forecast for ``(lat, lon)``, or ``None``.

    ``fetch(url) -> dict`` is the injectable network edge so tests stay
    offline. Best-effort: any exception, HTTP failure, or missing field
    returns ``None`` rather than raising.
    """
    fetch = fetch or _default_fetch
    try:
        points = fetch(f"https://api.weather.gov/points/{lat},{lon}")
        forecast_url = points["properties"]["forecast"]
        forecast = fetch(forecast_url)
        periods = forecast["properties"]["periods"]
    except Exception as exc:  # noqa: BLE001 - best effort; never raise
        logger.warning("weather: fetch failed: %s", exc)
        return None
    if not isinstance(periods, list) or not periods:
        logger.warning("weather: forecast carried no periods")
        return None

    now_part = _format_period(periods[0], with_name=False)
    if now_part is None:
        logger.warning("weather: first period was malformed")
        return None
    later_part = (
        _format_period(periods[1], with_name=True) if len(periods) > 1 else None
    )
    return f"{now_part}, {later_part}" if later_part else now_part
