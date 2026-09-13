"""鉴权判定测试：令牌可选但网络暴露时强制。"""
from __future__ import annotations

from web.api import auth_passes


def test_token_set_requires_match():
    assert auth_passes("secret", "127.0.0.1", "secret") is True
    assert auth_passes("secret", "0.0.0.0", "secret") is True
    assert auth_passes("secret", "127.0.0.1", "wrong") is False
    assert auth_passes("secret", "127.0.0.1", None) is False
    assert auth_passes("secret", "127.0.0.1", "") is False


def test_token_unset_loopback_allows_anonymous():
    for host in ("127.0.0.1", "localhost", "::1"):
        assert auth_passes("", host, None) is True
        assert auth_passes("", host, "") is True


def test_token_unset_network_exposure_rejected():
    for host in ("0.0.0.0", "192.168.1.5"):
        assert auth_passes("", host, None) is False
        assert auth_passes("", host, "anything") is False
