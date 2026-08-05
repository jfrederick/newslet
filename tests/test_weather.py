"""Tests for :mod:`newslet.weather` with a faked NWS fetch."""

from __future__ import annotations

import pytest

from newslet import weather


def _fake_fetch(points=None, forecast=None):
    calls: list[str] = []

    def fetch(url: str) -> dict:
        calls.append(url)
        if "api.weather.gov/points/" in url:
            return points if points is not None else {
                "properties": {"forecast": "https://api.weather.gov/gridpoints/OKX/1,2/forecast"}
            }
        return forecast if forecast is not None else {
            "properties": {
                "periods": [
                    {"name": "Today", "temperature": 78, "isDaytime": True,
                     "shortForecast": "Chance Light Rain"},
                    {"name": "Tonight", "temperature": 64, "isDaytime": False,
                     "shortForecast": "Mostly Clear"},
                ]
            }
        }

    fetch.calls = calls
    return fetch


def test_happy_path_formats_one_line():
    fetch = _fake_fetch()
    line = weather.fetch_weather(fetch=fetch)
    assert line == "today 78° chance light rain, tonight 64° mostly clear"
    assert fetch.calls[0] == (
        f"https://api.weather.gov/points/{weather.BROOKLYN_LAT},{weather.BROOKLYN_LON}"
    )
    assert fetch.calls[1].endswith("/forecast")


def test_custom_coordinates_reach_the_points_url():
    fetch = _fake_fetch()
    weather.fetch_weather(lat=51.5, lon=-0.12, fetch=fetch)
    assert fetch.calls[0] == "https://api.weather.gov/points/51.5,-0.12"


def test_single_period_renders_without_second_clause():
    fetch = _fake_fetch(forecast={
        "properties": {"periods": [
            {"name": "Today", "temperature": 80, "shortForecast": "Sunny"},
        ]}
    })
    assert weather.fetch_weather(fetch=fetch) == "today 80° sunny"


def test_fetch_error_returns_none():
    def boom(url):
        raise OSError("network down")

    assert weather.fetch_weather(fetch=boom) is None


def test_missing_forecast_url_returns_none():
    assert weather.fetch_weather(fetch=_fake_fetch(points={"properties": {}})) is None


@pytest.mark.parametrize(
    "forecast",
    [
        {"properties": {}},
        {"properties": {"periods": []}},
        {"properties": {"periods": [{"name": "Today"}]}},  # no temp/shortForecast
        {"properties": {"periods": [{"temperature": "hot", "shortForecast": "x"}]}},
        {"properties": {"periods": ["not-a-dict"]}},
        {"properties": {"periods": [
            {"name": "Today", "temperature": float("nan"), "shortForecast": "x"},
        ]}},
        {"properties": {"periods": [
            {"temperature": 70, "shortForecast": "sunny"},  # unnamed period
        ]}},
    ],
)
def test_malformed_forecast_returns_none(forecast):
    assert weather.fetch_weather(fetch=_fake_fetch(forecast=forecast)) is None
