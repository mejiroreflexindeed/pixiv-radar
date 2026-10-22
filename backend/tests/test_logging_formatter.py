"""JsonFormatter / TextFormatter 级脱敏回归测试。

覆盖 Codex 复现的嵌套 JSON 泄漏：先 json.dumps() 会把内层引号转义，
转义后的 \" 绕过正则。修复后必须先对 record.getMessage() 与异常文本
脱敏，再 dumps。
"""

import json
import logging
import sys

from app.core.logging import JsonFormatter, TextFormatter


def _make_record(
    msg: str, args=None, exc_info=None
) -> logging.LogRecord:
    return logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg=msg,
        args=args,
        exc_info=exc_info,
    )


# 1) 消息本身是 JSON 字符串并包含 Authorization（复现原文）


def test_json_message_with_authorization_no_leak():
    out = JsonFormatter().format(
        _make_record('{"Authorization": "Bearer top-secret", "user": "alice"}')
    )
    assert "top-secret" not in out
    assert "alice" in out


def test_json_message_nested_no_leak_and_valid_output():
    out = JsonFormatter().format(
        _make_record('{"Authorization": "Bearer top-secret"}')
    )
    payload = json.loads(out)  # 输出必须是合法 JSON
    assert "top-secret" not in payload["msg"]
    assert "top-secret" not in out


# 2) %s 参数是包含 Authorization / Cookie 的 dict


def test_dict_args_with_authorization_and_cookie_no_leak():
    out = JsonFormatter().format(
        _make_record(
            "headers: %s",
            args=({"Authorization": "Bearer xyz789", "Cookie": "session=abc123"},),
        )
    )
    assert "xyz789" not in out
    assert "abc123" not in out


# 3) 异常消息包含 refresh token


def test_exception_message_with_refresh_token_no_leak():
    try:
        raise RuntimeError("auth failed: refresh_token=supersecret123")
    except RuntimeError:
        record = _make_record("task failed", exc_info=sys.exc_info())
    out = JsonFormatter().format(record)
    assert "supersecret123" not in out


# 4) 输出字段结构不变


def test_json_formatter_field_structure_unchanged():
    payload = json.loads(JsonFormatter().format(_make_record("hello")))
    assert set(payload.keys()) == {"ts", "level", "logger", "msg"}


def test_json_formatter_exc_field_only_when_exception():
    try:
        raise ValueError("boom")
    except ValueError:
        record = _make_record("failed", exc_info=sys.exc_info())
    payload = json.loads(JsonFormatter().format(record))
    assert set(payload.keys()) == {"ts", "level", "logger", "msg", "exc"}
    assert "boom" in payload["exc"]


# TextFormatter 保持最终字符串脱敏


def test_text_formatter_still_redacts():
    out = TextFormatter().format(_make_record("Authorization: Bearer top-secret"))
    assert "top-secret" not in out
