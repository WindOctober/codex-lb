from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

import pytest

from app.core.clients.upstream import UpstreamProbeResult
from app.core.openai import provider_model_refresh
from app.db.models import ACCOUNT_PROVIDER_API_KEY, Account, AccountStatus

pytestmark = pytest.mark.unit


class _Result:
    def __init__(self, accounts: list[Account]) -> None:
        self._accounts = accounts

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[Account]:
        return self._accounts


class _Session:
    def __init__(self, accounts: list[Account]) -> None:
        self._accounts = accounts
        self.commits = 0

    async def execute(self, _stmt: object) -> _Result:
        return _Result(self._accounts)

    async def commit(self) -> None:
        self.commits += 1


class _Encryptor:
    def decrypt(self, encrypted: bytes) -> str:
        assert encrypted == b"encrypted"
        return "provider-key"


class _Cache:
    def __init__(self) -> None:
        self.invalidations = 0

    def invalidate(self) -> None:
        self.invalidations += 1


def _account(*, supported_models_json: str | None = '["gpt-5.4"]') -> Account:
    return Account(
        id="provider_1",
        chatgpt_account_id=None,
        email="DuckCoding (jp.duckcoding.com)",
        plan_type="api_key_provider",
        provider_kind=ACCOUNT_PROVIDER_API_KEY,
        upstream_base_url="https://jp.duckcoding.com/v1",
        upstream_wire_api="responses",
        upstream_priority=50,
        supported_models_json=supported_models_json,
        access_token_encrypted=b"encrypted",
        refresh_token_encrypted=b"",
        id_token_encrypted=b"",
        last_refresh=datetime.now(timezone.utc),
        status=AccountStatus.ACTIVE,
        deactivation_reason=None,
    )


def _patch_session(monkeypatch: pytest.MonkeyPatch, session: _Session) -> None:
    @asynccontextmanager
    async def fake_get_background_session() -> AsyncIterator[_Session]:
        yield session

    monkeypatch.setattr(provider_model_refresh, "get_background_session", fake_get_background_session)


@pytest.mark.asyncio
async def test_refresh_api_provider_model_snapshots_updates_changed_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    session = _Session([account])
    cache = _Cache()
    _patch_session(monkeypatch, session)
    monkeypatch.setattr(provider_model_refresh, "TokenEncryptor", _Encryptor)
    monkeypatch.setattr(provider_model_refresh, "get_account_selection_cache", lambda: cache)

    async def fake_probe(*, base_url: str, api_key: str) -> UpstreamProbeResult:
        assert base_url == "https://jp.duckcoding.com/v1"
        assert api_key == "provider-key"
        return UpstreamProbeResult(
            base_url="https://jp.duckcoding.com",
            wire_api="responses",
            supported_models=("gpt-5.5", "gpt-5.5-high"),
        )

    monkeypatch.setattr(provider_model_refresh, "probe_upstream_provider", fake_probe)

    result = await provider_model_refresh.refresh_api_provider_model_snapshots_on_startup()

    assert result.checked == 1
    assert result.refreshed == 1
    assert result.changed == 1
    assert result.failed == 0
    assert session.commits == 1
    assert cache.invalidations == 1
    assert account.upstream_base_url == "https://jp.duckcoding.com"
    assert account.supported_models_json == '["gpt-5.5", "gpt-5.5-high"]'


@pytest.mark.asyncio
async def test_refresh_api_provider_model_snapshots_keeps_snapshot_on_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    session = _Session([account])
    cache = _Cache()
    _patch_session(monkeypatch, session)
    monkeypatch.setattr(provider_model_refresh, "TokenEncryptor", _Encryptor)
    monkeypatch.setattr(provider_model_refresh, "get_account_selection_cache", lambda: cache)

    async def fake_probe(*, base_url: str, api_key: str) -> UpstreamProbeResult:
        del base_url, api_key
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(provider_model_refresh, "probe_upstream_provider", fake_probe)

    result = await provider_model_refresh.refresh_api_provider_model_snapshots_on_startup()

    assert result.checked == 1
    assert result.refreshed == 0
    assert result.changed == 0
    assert result.failed == 1
    assert session.commits == 0
    assert cache.invalidations == 0
    assert account.supported_models_json == '["gpt-5.4"]'
