from app.core.logging import redact


# === 等号形式 ===


def test_redact_refresh_token_equals():
    out = redact("refresh_token=abc123")
    assert "abc123" not in out
    assert "***" in out


def test_redact_api_key_equals():
    out = redact("api_key=abc123")
    assert "abc123" not in out


# === 冒号形式 ===


def test_redact_access_token_colon():
    out = redact("access_token: abc123")
    assert "abc123" not in out


# === JSON 形式 ===


def test_redact_json_quoted():
    out = redact('{"password": "s3cret", "user": "alice"}')
    assert "s3cret" not in out
    assert "alice" in out


def test_redact_password_colon_quoted():
    out = redact('"password": "secret"')
    assert "secret" not in out


# === 带空格值：头部类键必须整体脱敏，真实 secret 完全不在输出中 ===


def test_redact_authorization_bearer_full():
    out = redact("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.SECRET")
    assert "eyJhbGciOiJIUzI1NiJ9.SECRET" not in out
    assert "Bearer" not in out


def test_redact_proxy_authorization_full():
    out = redact("Proxy-Authorization: Basic dXNlcjpwYXNz")
    assert "dXNlcjpwYXNz" not in out
    assert "Basic" not in out


def test_redact_cookie_full():
    out = redact("Cookie: sessionid=abc123; theme=dark")
    assert "abc123" not in out
    assert "theme=dark" not in out


def test_redact_set_cookie_full():
    out = redact("Set-Cookie: sid=xyz789; Path=/; HttpOnly")
    assert "xyz789" not in out


def test_redact_authorization_dict_quoted():
    out = redact("{'Authorization': 'Bearer abc123'}")
    assert "abc123" not in out


# === 正常文本不受影响 ===


def test_redact_leaves_normal_text():
    assert redact("hello world") == "hello world"


def test_redact_non_string():
    assert redact(123) == 123
