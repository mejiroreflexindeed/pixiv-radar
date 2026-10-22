"""pytest 共享 fixture。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    """指向临时数据目录的测试客户端，隔离副作用，不联网。"""
    monkeypatch.setenv("RADAR_DB_PATH", str(tmp_path / "state" / "radar.db"))
    monkeypatch.setenv("RADAR_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RADAR_LOG_FORMAT", "text")
    get_settings.cache_clear()

    app = create_app()
    with TestClient(app) as c:
        yield c

    get_settings.cache_clear()
