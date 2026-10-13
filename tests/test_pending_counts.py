"""Tests for the read-only MTProto pending-count reader.

The telethon package is not installed in this environment, which is exactly
the "package missing" case the module is required to handle: these tests
exercise the guarded-import path for real, not a mocked substitute for it.
"""
import pytest

from telegram_mtproto import pending_counts
from config import settings


@pytest.fixture(autouse=True)
def _reset_client():
    pending_counts._client = None
    yield
    pending_counts._client = None


def test_telethon_is_not_installed_here():
    # Ground truth for every other assertion in this file: if this ever
    # flips to True, the "package missing" tests below stop testing the
    # guarded-import path and start testing a different thing entirely.
    assert pending_counts._TELETHON_AVAILABLE is False


def test_mtproto_available_false_without_telethon_package(monkeypatch):
    monkeypatch.setattr(pending_counts, "_TELETHON_AVAILABLE", False)
    monkeypatch.setattr(settings, "telegram_pending_enabled", True)
    monkeypatch.setattr(settings, "telegram_api_id", 123)
    monkeypatch.setattr(settings, "telegram_api_hash", "hash")
    assert pending_counts.mtproto_available() is False


def test_mtproto_available_false_without_credentials(monkeypatch):
    monkeypatch.setattr(pending_counts, "_TELETHON_AVAILABLE", True)
    monkeypatch.setattr(settings, "telegram_pending_enabled", True)
    monkeypatch.setattr(settings, "telegram_api_id", None)
    monkeypatch.setattr(settings, "telegram_api_hash", "hash")
    assert pending_counts.mtproto_available() is False


def test_mtproto_available_false_when_disabled_via_settings(monkeypatch):
    monkeypatch.setattr(pending_counts, "_TELETHON_AVAILABLE", True)
    monkeypatch.setattr(settings, "telegram_pending_enabled", False)
    monkeypatch.setattr(settings, "telegram_api_id", 123)
    monkeypatch.setattr(settings, "telegram_api_hash", "hash")
    assert pending_counts.mtproto_available() is False


def test_mtproto_available_true_when_everything_set(monkeypatch):
    monkeypatch.setattr(pending_counts, "_TELETHON_AVAILABLE", True)
    monkeypatch.setattr(settings, "telegram_pending_enabled", True)
    monkeypatch.setattr(settings, "telegram_api_id", 123)
    monkeypatch.setattr(settings, "telegram_api_hash", "hash")
    assert pending_counts.mtproto_available() is True


@pytest.mark.asyncio
async def test_fetch_pending_counts_disabled_returns_none_for_every_chat(monkeypatch):
    monkeypatch.setattr(pending_counts, "mtproto_available", lambda: False)
    result = await pending_counts.fetch_pending_counts([1, 2])
    assert result == {1: None, 2: None}


@pytest.mark.asyncio
async def test_fetch_pending_counts_empty_input_short_circuits(monkeypatch):
    monkeypatch.setattr(pending_counts, "mtproto_available", lambda: True)
    result = await pending_counts.fetch_pending_counts([])
    assert result == {}


class _FakeFullChat:
    def __init__(self, value):
        self.requests_pending = value


class _FakeFull:
    def __init__(self, value):
        self.full_chat = _FakeFullChat(value)


class _FakeRequest:
    def __init__(self, channel):
        self.channel = channel


class _FakeChannelsNamespace:
    @staticmethod
    def GetFullChannelRequest(channel):
        return _FakeRequest(channel)


class _FakeFunctions:
    channels = _FakeChannelsNamespace


class _FakeClient:
    def __init__(self, responses):
        self._responses = responses

    async def __call__(self, request):
        value = self._responses[request.channel]
        if isinstance(value, Exception):
            raise value
        return _FakeFull(value)


@pytest.mark.asyncio
async def test_fetch_pending_counts_per_chat_error_is_isolated(monkeypatch):
    monkeypatch.setattr(pending_counts, "mtproto_available", lambda: True)
    monkeypatch.setattr(pending_counts, "functions", _FakeFunctions)
    fake_client = _FakeClient({1: 31, 2: RuntimeError("boom"), 3: None})

    async def _fake_get_client():
        return fake_client

    monkeypatch.setattr(pending_counts, "_get_client", _fake_get_client)

    result = await pending_counts.fetch_pending_counts([1, 2, 3])
    assert result == {1: 31, 2: None, 3: None}


@pytest.mark.asyncio
async def test_fetch_pending_counts_client_failure_yields_none_for_all(monkeypatch):
    monkeypatch.setattr(pending_counts, "mtproto_available", lambda: True)

    async def _broken_get_client():
        raise RuntimeError("no connection")

    monkeypatch.setattr(pending_counts, "_get_client", _broken_get_client)

    result = await pending_counts.fetch_pending_counts([1, 2])
    assert result == {1: None, 2: None}


@pytest.mark.asyncio
async def test_close_client_noop_when_never_opened():
    pending_counts._client = None
    await pending_counts.close_client()  # must not raise
    assert pending_counts._client is None


@pytest.mark.asyncio
async def test_close_client_swallows_disconnect_errors():
    class _BoomClient:
        async def disconnect(self):
            raise RuntimeError("boom")

    pending_counts._client = _BoomClient()
    await pending_counts.close_client()  # must not raise
    assert pending_counts._client is None
