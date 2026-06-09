"""Additional tests for :mod:`newslet.handlers.inbound` — SSRF guards,
_follow_link, _PublicOnlyRedirect, and edge cases.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import pytest

from newslet.handlers.inbound import _follow_link, _is_public_url, _PublicOnlyRedirect

# --- _is_public_url edge cases ---


def test_is_public_url_rejects_no_hostname():
    assert _is_public_url("http:///path") is False


def test_is_public_url_rejects_ipv6_loopback():
    # socket.getaddrinfo for "::1" should resolve to a non-global address
    with patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("::1", 0))]):
        assert _is_public_url("http://[::1]/confirm") is False


def test_is_public_url_handles_dns_failure():
    with patch("socket.getaddrinfo", side_effect=OSError("DNS failed")):
        assert _is_public_url("http://nosuchhost.example/confirm") is False


def test_is_public_url_handles_invalid_ip_in_addrinfo():
    with patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("not-an-ip", 0))]):
        assert _is_public_url("http://weird.example/confirm") is False


# --- _PublicOnlyRedirect ---


def test_public_only_redirect_allows_public_target():
    handler = _PublicOnlyRedirect()
    req = urllib.request.Request("http://origin.example.com")
    with patch("newslet.handlers.inbound._is_public_url", return_value=True):
        result = handler.redirect_request(req, None, 302, "Found", {}, "http://safe.example.com/ok")
    assert result is not None


def test_public_only_redirect_blocks_private_target():
    handler = _PublicOnlyRedirect()
    req = urllib.request.Request("http://origin.example.com")
    with (
        patch("newslet.handlers.inbound._is_public_url", return_value=False),
        pytest.raises(urllib.error.URLError, match="unsafe redirect"),
    ):
        handler.redirect_request(
            req, None, 302, "Found", {}, "http://10.0.0.1/internal"
        )


# --- _follow_link ---


def test_follow_link_refuses_non_public_url():
    with patch("newslet.handlers.inbound._is_public_url", return_value=False):
        assert _follow_link("http://169.254.169.254/metadata") is False


def test_follow_link_returns_true_on_success():
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__ = lambda s: mock_resp
    mock_resp.__exit__ = lambda s, *a: None

    with (
        patch("newslet.handlers.inbound._is_public_url", return_value=True),
        patch("urllib.request.build_opener") as mock_opener,
    ):
        mock_opener.return_value.open.return_value = mock_resp
        assert _follow_link("https://confirm.example.com/token") is True


def test_follow_link_returns_false_on_network_error():
    with (
        patch("newslet.handlers.inbound._is_public_url", return_value=True),
        patch("urllib.request.build_opener") as mock_opener,
    ):
        mock_opener.return_value.open.side_effect = OSError("timeout")
        assert _follow_link("https://confirm.example.com/token") is False
