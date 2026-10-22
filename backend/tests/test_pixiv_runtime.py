import pytest

from app.core.credentials import InMemoryCredentialStore
from app.pixiv.exceptions import AuthFailed, TemporaryFailure
from app.pixiv.runtime import CredentialedPixivAdapter


class CountingStore(InMemoryCredentialStore):
    def __init__(self, token=None):
        super().__init__(token)
        self.reads = 0

    def get(self):
        self.reads += 1
        return super().get()


class Api:
    def webview_novel(self, novel_id):
        return {"text": "本文"}


def test_runtime_gate_precedes_credential_read():
    store = CountingStore("secret")
    adapter = CredentialedPixivAdapter(store, allow_real=lambda: False)
    assert "secret" not in repr(adapter)
    with pytest.raises(TemporaryFailure):
        adapter.novel_text(1)
    assert store.reads == 0


def test_runtime_missing_token_fails_closed():
    store = CountingStore()
    adapter = CredentialedPixivAdapter(store, allow_real=lambda: True)
    with pytest.raises(AuthFailed):
        adapter.novel_text(1)
    assert store.reads == 1


def test_runtime_authenticates_once_per_instance():
    store = CountingStore("secret")
    tokens = []
    adapter = CredentialedPixivAdapter(
        store,
        allow_real=lambda: True,
        api_factory=lambda token: tokens.append(token) or Api(),
    )
    assert adapter.novel_text(1).text == "本文"
    assert adapter.novel_text(2).text == "本文"
    assert tokens == ["secret"]
    assert store.reads == 1
