"""路由层共享的 JSON 字段解码工具。"""

from __future__ import annotations

import json


def decode_reason_codes(item: dict, field: str) -> dict:
    raw = item.pop(field, None)
    if raw is None:
        item[field] = []
        return item
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        value = []
    item[field] = value if isinstance(value, list) else []
    return item
