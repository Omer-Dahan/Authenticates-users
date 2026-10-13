"""Tests for the pending-count cache and its refresh loop.

The render path (get_cached_pending_count) must never touch the network -
only the refresh loop is allowed to call fetch_pending_counts.
"""
from datetime import datetime, timedelta, timezone

import pytest

from config import settings
from telegram_mtproto import pending_cache


@pytest.fixture(autouse=True)
def _clear_cache():
    pending_cache._cache.clear()
    yield
    pending_cache._cache.clear()


def test_get_cached_pending_count_missing_entry_returns_none():
    assert pending_cache.get_cached_pending_count(999) is None


def test_get_cached_pending_count_returns_fresh_value():
    pending_cache._cache[111] = (31, datetime.now(timezone.utc))
    assert pending_cache.get_cached_pending_count(111) == 31


def test_get_cached_pending_count_omits_none_value():
    pending_cache._cache[111] = (None, datetime.now(timezone.utc))
    assert pending_cache.get_cached_pending_count(111) is None


def test_get_cached_pending_count_omits_stale_value(monkeypatch):
    monkeypatch.setattr(settings, "telegram_pending_refresh_seconds", 600)
    old = datetime.now(timezone.utc) - timedelta(seconds=10_000)
    pending_cache._cache[111] = (31, old)
    assert pending_cache.get_cached_pending_count(111) is None


@pytest.mark.asyncio
async def test_start_pending_refresh_loop_returns_immediately_when_unavailable(monkeypatch):
    monkeypatch.setattr(pending_cache, "mtproto_available", lambda: False)
    # If this did not return on its own, the test would hang forever.
    await pending_cache.start_pending_refresh_loop()


@pytest.mark.asyncio
async def test_refresh_once_populates_cache_from_active_groups(test_session_factory, sample_group, monkeypatch):
    monkeypatch.setattr(pending_cache, "AsyncSessionLocal", test_session_factory)

    async def _fake_fetch(chat_ids):
        return {chat_id: 7 for chat_id in chat_ids}

    monkeypatch.setattr(pending_cache, "fetch_pending_counts", _fake_fetch)

    await pending_cache._refresh_once()

    assert pending_cache.get_cached_pending_count(sample_group.chat_id) == 7


@pytest.mark.asyncio
async def test_refresh_once_records_none_on_per_chat_failure(test_session_factory, sample_group, monkeypatch):
    monkeypatch.setattr(pending_cache, "AsyncSessionLocal", test_session_factory)

    async def _fake_fetch(chat_ids):
        return {chat_id: None for chat_id in chat_ids}

    monkeypatch.setattr(pending_cache, "fetch_pending_counts", _fake_fetch)

    await pending_cache._refresh_once()

    assert pending_cache.get_cached_pending_count(sample_group.chat_id) is None


@pytest.mark.asyncio
async def test_refresh_once_skips_network_call_when_no_active_groups(test_session_factory, monkeypatch):
    monkeypatch.setattr(pending_cache, "AsyncSessionLocal", test_session_factory)

    called = False

    async def _fake_fetch(chat_ids):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(pending_cache, "fetch_pending_counts", _fake_fetch)

    await pending_cache._refresh_once()

    assert called is False
