"""CredentialStore 契约测试：内存实现与 keyring 包装（全部离线）。"""

import keyring.errors
import pytest

from app.core.credentials import (
    KEYRING_KEY,
    KEYRING_SERVICE,
    CredentialUnavailable,
    CredentialStore,
    InMemoryCredentialStore,
)
from app.core.keyring_store import KeyringCredentialStore


# ---- 内存实现 ----


def test_in_memory_set_get_delete():
    store = InMemoryCredentialStore()
    assert store.get() is None
    store.set("tok123")
    assert store.get() == "tok123"
    store.delete()
    assert store.get() is None


def test_in_memory_repr_never_contains_token():
    store = InMemoryCredentialStore("super-secret-token")
    assert "super-secret-token" not in repr(store)


def test_in_memory_satisfies_protocol():
    assert isinstance(InMemoryCredentialStore(), CredentialStore)


# ---- fake keyring ----


def make_fake_keyring(module_name="keyring.backends.Windows", raise_on_delete=False):
    class FakeKeyringModule:
        def __init__(self):
            self.calls = []
            self._password = None
            self._raise_on_delete = raise_on_delete

        def get_keyring(self):
            self.calls.append(("get_keyring",))
            return type("Backend", (), {"__module__": module_name})()

        def get_password(self, service, key):
            self.calls.append(("get_password", service, key))
            return self._password

        def set_password(self, service, key, password):
            self.calls.append(("set_password", service, key, password))
            self._password = password

        def delete_password(self, service, key):
            self.calls.append(("delete_password", service, key))
            if self._raise_on_delete:
                raise keyring.errors.PasswordDeleteError("no such password")
            self._password = None

    return FakeKeyringModule()


# ---- keyring 包装 ----


def test_keyring_store_uses_only_fixed_item():
    fake = make_fake_keyring()
    store = KeyringCredentialStore(keyring_module=fake)

    assert store.get() is None
    store.set("tok-abc")
    assert store.get() == "tok-abc"
    store.delete()
    assert store.get() is None

    # 所有凭据操作只针对固定 service/key
    for call in fake.calls:
        if call[0] in ("get_password", "set_password", "delete_password"):
            assert call[1] == KEYRING_SERVICE
            assert call[2] == KEYRING_KEY

    # 不存在任何枚举类调用：只允许出现这四个操作
    operation_names = {call[0] for call in fake.calls}
    assert operation_names <= {
        "get_keyring",
        "get_password",
        "set_password",
        "delete_password",
    }


def test_keyring_store_accepts_safe_backend():
    fake = make_fake_keyring(module_name="keyring.backends.Windows")
    store = KeyringCredentialStore(keyring_module=fake)
    assert store.get() is None


def test_keyring_store_fail_closed_on_plaintext_backend():
    fake = make_fake_keyring(module_name="keyrings.alt.file")
    with pytest.raises(CredentialUnavailable):
        KeyringCredentialStore(keyring_module=fake)


def test_keyring_store_fail_closed_on_missing_backend():
    fake = make_fake_keyring(module_name="keyring.backend.fail")
    with pytest.raises(CredentialUnavailable):
        KeyringCredentialStore(keyring_module=fake)


def test_keyring_store_fail_closed_when_backend_raises():
    class BrokenKeyring:
        def get_keyring(self):
            raise RuntimeError("backend discovery failed")

    with pytest.raises(CredentialUnavailable):
        KeyringCredentialStore(keyring_module=BrokenKeyring())


def test_keyring_store_repr_no_token():
    fake = make_fake_keyring()
    store = KeyringCredentialStore(keyring_module=fake)
    store.set("secret-xyz")
    assert "secret-xyz" not in repr(store)


def test_keyring_store_delete_ignores_missing_item():
    fake = make_fake_keyring(raise_on_delete=True)
    store = KeyringCredentialStore(keyring_module=fake)
    store.delete()  # 项不存在时不抛异常
