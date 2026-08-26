import pytest

from app.api.dependencies import is_lan_client
from app.core.config import Settings


def test_lan_peer_filter_accepts_loopback_and_rfc1918_only() -> None:
    assert is_lan_client("127.0.0.1")
    assert is_lan_client("192.168.1.20")
    assert is_lan_client("10.12.0.4")
    assert is_lan_client("172.16.4.9")
    assert not is_lan_client("8.8.8.8")
    assert not is_lan_client("100.64.0.5")
    assert not is_lan_client(None)


def test_insecure_lan_bind_requires_explicit_lan_only() -> None:
    with pytest.raises(ValueError, match="LAN_ONLY"):
        Settings(host="0.0.0.0")
    settings = Settings(host="0.0.0.0", lan_only=True)
    assert settings.lan_only is True


def test_public_host_is_always_rejected() -> None:
    with pytest.raises(ValueError, match="RFC1918"):
        Settings(host="8.8.8.8", lan_only=True)
