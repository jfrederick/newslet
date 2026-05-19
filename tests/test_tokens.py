"""Tests for :mod:`newslet.tokens`."""

from __future__ import annotations

import pytest

from newslet.config import settings
from newslet.tokens import sign, verify


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-anthropic")
    monkeypatch.setenv("RESEND_API_KEY", "dummy-resend")
    monkeypatch.setenv("FROM_EMAIL", "from@example.com")
    monkeypatch.setenv("TO_EMAIL", "to@example.com")
    monkeypatch.setenv("ADMIN_TOKEN", "dummy-admin")
    monkeypatch.setenv("SIGNING_KEY", "dummy-signing-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    settings.cache_clear()
    yield
    settings.cache_clear()


URL = "https://example.com/post/123"
DATE = "2026-05-17"


def test_roundtrip() -> None:
    token = sign(URL, DATE)
    assert verify(URL, DATE, token) is True


def test_token_has_no_padding() -> None:
    token = sign(URL, DATE)
    assert "=" not in token


def test_tampered_token_fails() -> None:
    token = sign(URL, DATE)
    # Flip a character; keep within urlsafe alphabet.
    tampered = ("A" if token[0] != "A" else "B") + token[1:]
    assert verify(URL, DATE, tampered) is False


def test_wrong_date_fails() -> None:
    token = sign(URL, DATE)
    assert verify(URL, "2026-05-18", token) is False


def test_wrong_url_fails() -> None:
    token = sign(URL, DATE)
    assert verify("https://example.com/other", DATE, token) is False


def test_garbage_token_non_base64() -> None:
    assert verify(URL, DATE, "!!!not-base64!!!") is False


def test_garbage_token_empty() -> None:
    assert verify(URL, DATE, "") is False


def test_garbage_token_random_text() -> None:
    assert verify(URL, DATE, "hello world this is not a token") is False
