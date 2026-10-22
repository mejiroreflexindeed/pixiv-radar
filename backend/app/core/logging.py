"""结构化日志 + 凭据字段脱敏。

任何日志消息在写出前都经过 redact()，避免 token / password / secret /
authorization / cookie 等敏感字段进入日志或控制台。

redact() 分三段处理（顺序敏感）：
1. 头部类键的无引号值（可含空格，直到行尾）：Authorization: Bearer xxx /
   Proxy-Authorization / Cookie / Set-Cookie；值以引号开头时跳过，交给第 2 段精确处理，
   避免把同行的其他正常字段一并吞掉；
2. 引号包裹的值（可含空格）："password": "s3cret" / 'Authorization': 'Bearer xyz'；
3. 通用单 token 值：refresh_token=abc / api_key: abc。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

_HEADER_KEYS = r"(?:authorization|proxy[-_]authorization|cookie|set[-_]cookie)"

_TOKEN_KEYS = (
    r"(?:refresh[_-]?token|access[_-]?token|password|passwd|secret|"
    r"api[_-]?key|apikey|token)"
)

_KEY_ALT = rf"(?:{_HEADER_KEYS}|{_TOKEN_KEYS})"

# 1) 引号包裹的值（可含空格）
_QUOTED_VALUE_RE = re.compile(
    rf'(?i)((?<![a-z0-9_])[\'"]?{_KEY_ALT}[\'"]?\s*[=:]\s*)'
    rf'("[^"]*"|\'[^\']*\')'
)

# 2) 头部类键的无引号值：整体脱敏到行尾；值以引号开头时交给第 1 段精确处理。
#    注意 (?!\s*['"]) 必须把空白放进断言：否则主模式 \s* 回退后断言会落在
#    空格上被绕过，把带引号头部之后的所有内容一并吞掉。
_HEADER_EOL_RE = re.compile(
    rf'(?i)((?<![a-z0-9_])[\'"]?{_HEADER_KEYS}[\'"]?\s*[=:]\s*)(?!\s*[\'"]).*$',
    re.MULTILINE,
)

# 3) 通用单 token 值
_TOKEN_VALUE_RE = re.compile(
    rf'(?i)((?<![a-z0-9_])[\'"]?{_TOKEN_KEYS}[\'"]?\s*[=:]\s*)'
    rf'[^\s,;}}\]]+'
)

_REDACTED = "***"


def redact(text: object) -> object:
    """对任意对象做敏感字段脱敏；非字符串原样返回。"""
    if not isinstance(text, str):
        return text
    text = _HEADER_EOL_RE.sub(lambda m: m.group(1) + _REDACTED, text)
    text = _QUOTED_VALUE_RE.sub(lambda m: m.group(1) + _REDACTED, text)
    text = _TOKEN_VALUE_RE.sub(lambda m: m.group(1) + _REDACTED, text)
    return text


def redacted_error_text(exc: BaseException, limit: int = 1000) -> str:
    """异常转文本并脱敏、截断，用于落库 / 展示（保证 secret 不进入存储）。"""
    text = str(redact(str(exc)) or "").strip()
    if len(text) > limit:
        text = text[:limit] + "..."
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # 必须先脱敏再 dumps：dumps 会把嵌套 JSON 的引号转义，
        # 转义后的 \" 会绕过正则，导致 secret 泄漏到输出中。
        payload: dict = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(level: str = "INFO", fmt: str = "json") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())

    handler = logging.StreamHandler()
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            TextFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )

    # 清掉可能重复注册的 handler（测试多次调用 setup_logging 时避免重复）
    root.handlers = [handler]

    # 降低访问日志噪音
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
