"""Tests for :mod:`newslet.config` — settings loading and secret resolution."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from newslet.config import Settings, _required, _secret, settings


@pytest.fixture(autouse=True)
def clear_cache():
    settings.cache_clear()
    yield
    settings.cache_clear()


def test_required_raises_when_env_var_missing(monkeypatch):
    monkeypatch.delenv("NONEXISTENT_VAR_XYZ", raising=False)
    with pytest.raises(RuntimeError, match="Missing required env var"):
        _required("NONEXISTENT_VAR_XYZ")


def test_required_returns_value_when_set(monkeypatch):
    monkeypatch.setenv("MY_VAR", "hello")
    assert _required("MY_VAR") == "hello"


def test_secret_prefers_env_var(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-value")
    assert _secret("ANTHROPIC_API_KEY", "anthropic-api-key") == "env-value"


def test_secret_falls_back_to_ssm(monkeypatch):
    monkeypatch.delenv("MY_SECRET", raising=False)
    monkeypatch.setenv("NEWSLET_SSM_PREFIX", "/test")

    mock_ssm = MagicMock()
    mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "ssm-value"}}

    with patch("newslet.config._ssm_client", return_value=mock_ssm):
        result = _secret("MY_SECRET", "my-secret")

    assert result == "ssm-value"
    mock_ssm.get_parameter.assert_called_once_with(
        Name="/test/my-secret", WithDecryption=True
    )


def test_secret_raises_when_ssm_fails(monkeypatch):
    monkeypatch.delenv("MY_SECRET", raising=False)
    monkeypatch.setenv("NEWSLET_SSM_PREFIX", "/newslet")

    mock_ssm = MagicMock()
    mock_ssm.get_parameter.side_effect = Exception("AccessDenied")

    with (
        patch("newslet.config._ssm_client", return_value=mock_ssm),
        pytest.raises(RuntimeError, match="SSM lookup.*failed"),
    ):
        _secret("MY_SECRET", "my-secret")


def test_settings_constructs_from_env(monkeypatch):
    env = {
        "ANTHROPIC_API_KEY": "ak",
        "RESEND_API_KEY": "rk",
        "FROM_EMAIL": "f@x.com",
        "TO_EMAIL": "t@x.com",
        "ADMIN_TOKEN": "at",
        "SIGNING_KEY": "sk",
        "PUBLIC_BASE_URL": "https://api.example.com",
        "DIGEST_FUNCTION_NAME": "my-fn",
        "MAIL_DOMAIN": "inbox.example.com",
        "INBOX_BUCKET": "my-bucket",
        "AWS_REGION": "eu-west-1",
        "TABLE_FEEDS": "f",
        "TABLE_PROFILE": "p",
        "TABLE_SEEN": "s",
        "TABLE_ISSUES": "i",
        "TABLE_FEEDBACK": "fb",
        "TABLE_SUBSCRIPTIONS": "sub",
        "TABLE_INBOX": "inb",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    s = settings()
    assert isinstance(s, Settings)
    assert s.anthropic_api_key == "ak"
    assert s.aws_region == "eu-west-1"
    assert s.mail_domain == "inbox.example.com"
    assert s.digest_function_name == "my-fn"
