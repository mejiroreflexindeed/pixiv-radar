import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_defaults_are_localhost():
    s = Settings(_env_file=None)
    assert s.host == "127.0.0.1"
    assert s.port == 8000
    assert s.app_name == "pixiv-radar"
    assert s.discovery_mode == "demo"
    assert s.discovery_interval_minutes == 0
    assert s.discovery_tag == "ウマ娘"


def test_env_override(monkeypatch):
    monkeypatch.setenv("RADAR_HOST", "127.0.0.1")
    monkeypatch.setenv("RADAR_PORT", "9000")
    s = Settings(_env_file=None)
    assert s.host == "127.0.0.1"
    assert s.port == 9000


def test_cors_origins_only_loopback():
    s = Settings(_env_file=None)
    for origin in s.cors_origins:
        assert "127.0.0.1" in origin or "localhost" in origin


@pytest.mark.parametrize(
    "ok_host",
    ["127.0.0.1", "127.0.0.2", "127.255.255.254", "localhost", "::1"],
)
def test_accepts_loopback_host(ok_host):
    s = Settings(_env_file=None, host=ok_host)
    assert s.host == ok_host


@pytest.mark.parametrize(
    "bad_host",
    [
        "0.0.0.0",
        "192.168.1.10",
        "10.0.0.5",
        "172.16.0.1",
        "172.31.255.254",
        "8.8.8.8",
        "::",
        "fe80::1",
        "example.com",
        "",
    ],
)
def test_rejects_non_loopback_host(bad_host):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, host=bad_host)


def test_rejects_non_loopback_host_from_env(monkeypatch):
    monkeypatch.setenv("RADAR_HOST", "0.0.0.0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_discovery_mode_is_strict(monkeypatch):
    monkeypatch.setenv("RADAR_DISCOVERY_MODE", "real")
    assert Settings(_env_file=None).discovery_mode == "real"
    monkeypatch.setenv("RADAR_DISCOVERY_MODE", "unexpected")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize("value", [-1, 1, 14, 1441])
def test_discovery_schedule_interval_is_conservative(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, discovery_interval_minutes=value)
    assert Settings(_env_file=None, discovery_interval_minutes=15).discovery_interval_minutes == 15


def test_discovery_tag_is_trimmed_and_bounded():
    assert Settings(_env_file=None, discovery_tag="  ウマ娘  ").discovery_tag == "ウマ娘"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, discovery_tag=" ")
