import os

import pytest

from zerocrm.serve import _Handler


class _FakeHeaders(dict):
    def get(self, key, default=""):
        return super().get(key, default)


def _handler(path, headers=None):
    h = _Handler.__new__(_Handler)
    h.path = path
    h.headers = _FakeHeaders(headers or {})
    return h


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("ZEROCRM_TRIGGER_TOKEN", "s3cret")
    yield


def test_authorized_rejects_missing_token():
    assert _handler("/contacts")._authorized() is False


def test_authorized_rejects_wrong_header_token():
    assert _handler("/contacts", {"X-Zerocrm-Token": "wrong"})._authorized() is False


def test_authorized_accepts_correct_header_token():
    assert _handler("/contacts", {"X-Zerocrm-Token": "s3cret"})._authorized() is True


def test_authorized_accepts_correct_query_token_for_plain_browser_get():
    assert _handler("/contacts?token=s3cret")._authorized() is True


def test_authorized_rejects_wrong_query_token():
    assert _handler("/contacts?token=nope")._authorized() is False


def test_authorized_rejects_everyone_when_no_server_token_configured(monkeypatch):
    monkeypatch.delenv("ZEROCRM_TRIGGER_TOKEN", raising=False)
    assert _handler("/contacts", {"X-Zerocrm-Token": "s3cret"})._authorized() is False
