"""时间工具：所有持久化时间统一为带时区的 ISO 8601 UTC。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utc_now_iso() -> str:
    """当前 UTC 时间的 ISO 8601 字符串（带 +00:00 偏移）。"""
    return datetime.now(timezone.utc).isoformat()


def add_seconds_iso(iso: str, seconds: float) -> str:
    """在 ISO 时间上增加秒数（用于租约过期时间计算）。"""
    return (datetime.fromisoformat(iso) + timedelta(seconds=seconds)).isoformat()


def normalize_utc_iso(value: str | None) -> str | None:
    """把 ISO 8601 时间归一为带 +00:00 偏移的 UTC 字符串。

    - 支持带偏移（如 "+09:00"）与 "Z" 后缀；
    - 无时区信息时按 UTC 处理；
    - 无法解析时返回 None，绝不把非 UTC 字符串写入 UTC 字段。
    """
    if not value:
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()
