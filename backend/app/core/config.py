"""应用配置：环境变量 / .env 覆盖，默认值保持本地安全。"""

from __future__ import annotations

from functools import lru_cache
from ipaddress import ip_address
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录：backend/app/core/config.py -> 上溯 3 级到 D:\codex\12
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _is_loopback(host: str) -> bool:
    """只允许回环地址（127.0.0.0/8、localhost、::1）。"""
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RADAR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "pixiv-radar"
    version: str = "0.1.0"

    # 本地单用户：后端只监听回环地址，拒绝 0.0.0.0 / 局域网等非回环地址
    host: str = "127.0.0.1"
    port: int = 8000

    log_level: str = "INFO"
    log_format: str = "json"  # "json" | "text"

    # 默认永远使用离线 Demo。真实发现必须同时显式设置 mode=real 与
    # RADAR_AUTH_ALLOW_REAL=1，且凭据库中存在本项目固定项。
    discovery_mode: Literal["demo", "real"] = "demo"
    # 0=关闭。只有真实入口显式设置后才启动进程内低频调度；至少 15 分钟，
    # 且启动后先等待一个完整周期，不在应用启动时访问 Pixiv。
    discovery_interval_minutes: int = 0
    discovery_tag: str = "ウマ娘"

    # 运行数据目录与 SQLite 路径（默认在项目根 data/ 下，不进入版本控制）
    data_dir: Path = PROJECT_ROOT / "data"
    db_path: Path = PROJECT_ROOT / "data" / "state" / "radar.db"

    @field_validator("host")
    @classmethod
    def _validate_host(cls, value: str) -> str:
        if not _is_loopback(value):
            raise ValueError(
                "host 只允许回环地址（127.0.0.0/8、localhost、::1），"
                f"收到: {value!r}"
            )
        return value

    @field_validator("discovery_interval_minutes")
    @classmethod
    def _validate_discovery_interval(cls, value: int) -> int:
        if value != 0 and not 15 <= value <= 1440:
            raise ValueError("discovery_interval_minutes 必须为 0 或 15..1440")
        return value

    @field_validator("discovery_tag")
    @classmethod
    def _validate_discovery_tag(cls, value: str) -> str:
        normalized = value.strip()
        if not 1 <= len(normalized) <= 100:
            raise ValueError("discovery_tag 必须包含 1..100 个字符")
        return normalized

    @property
    def cors_origins(self) -> list[str]:
        return [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
