from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from app.core.auth import generate_unique_account_id
from app.core.utils.time import naive_utc_to_epoch, utcnow
from app.db.models import UsageHistory
from app.db.session import SessionLocal
from app.dependencies import ProxyContext, get_proxy_context


@dataclass(frozen=True, slots=True)
class _FakeAccountRuntimeSnapshot:
    sessions: int = 0
    pending_requests: int = 0
    queued_requests: int = 0
    busy_sessions: int = 0
    codex_sessions: int = 0
    reconnect_requested_sessions: int = 0


class _FakeProxyService:
    def __init__(self, runtime: dict[str, _FakeAccountRuntimeSnapshot]) -> None:
        self._runtime = runtime

    async def get_http_bridge_account_runtime_snapshot(self) -> dict[str, _FakeAccountRuntimeSnapshot]:
        return self._runtime


pytestmark = pytest.mark.integration


def _encode_jwt(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return f"header.{body}.sig"


async def _seed_usage_history(account_id: str, *, window: str, used_percent: float, window_minutes: int) -> None:
    now = utcnow()
    async with SessionLocal() as session:
        session.add(
            UsageHistory(
                account_id=account_id,
                used_percent=used_percent,
                window=window,
                window_minutes=window_minutes,
                reset_at=naive_utc_to_epoch(now + timedelta(minutes=window_minutes)),
                recorded_at=now,
            )
        )
        await session.commit()


async def _import_test_account(async_client, *, raw_account_id: str, email: str) -> str:
    payload = {
        "email": email,
        "chatgpt_account_id": raw_account_id,
        "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"},
    }
    auth_json = {
        "tokens": {
            "idToken": _encode_jwt(payload),
            "accessToken": f"access-{raw_account_id}",
            "refreshToken": f"refresh-{raw_account_id}",
            "accountId": raw_account_id,
        },
    }
    response = await async_client.post(
        "/api/accounts/import",
        files={"auth_json": ("auth.json", json.dumps(auth_json), "application/json")},
    )
    assert response.status_code == 200
    return generate_unique_account_id(raw_account_id, email)


@pytest.mark.asyncio
async def test_import_and_list_accounts(async_client):
    email = "tester@example.com"
    raw_account_id = "acc_explicit"
    payload = {
        "email": email,
        "chatgpt_account_id": "acc_payload",
        "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"},
    }
    auth_json = {
        "tokens": {
            "idToken": _encode_jwt(payload),
            "accessToken": "access",
            "refreshToken": "refresh",
            "accountId": raw_account_id,
        },
    }

    expected_account_id = generate_unique_account_id(raw_account_id, email)
    files = {"auth_json": ("auth.json", json.dumps(auth_json), "application/json")}
    response = await async_client.post("/api/accounts/import", files=files)
    assert response.status_code == 200
    data = response.json()
    assert data["accountId"] == expected_account_id
    assert data["email"] == email
    assert data["planType"] == "plus"

    list_response = await async_client.get("/api/accounts")
    assert list_response.status_code == 200
    accounts = list_response.json()["accounts"]
    matched = next((account for account in accounts if account["accountId"] == expected_account_id), None)
    assert matched is not None
    assert matched["fastServiceTierEnabled"] is False


@pytest.mark.asyncio
async def test_account_quota_endpoint_returns_usage_and_runtime(async_client, app_instance):
    account_id = await _import_test_account(
        async_client,
        raw_account_id="acc_quota_status",
        email="quota-status@example.com",
    )
    await _seed_usage_history(account_id, window="primary", used_percent=25.0, window_minutes=300)
    await _seed_usage_history(account_id, window="secondary", used_percent=40.0, window_minutes=10080)

    async def _fake_proxy_context() -> ProxyContext:
        return ProxyContext(
            service=_FakeProxyService(
                {
                    account_id: _FakeAccountRuntimeSnapshot(
                        sessions=1,
                        pending_requests=1,
                        busy_sessions=1,
                    )
                }
            )
        )

    app_instance.dependency_overrides[get_proxy_context] = _fake_proxy_context
    try:
        response = await async_client.get(f"/api/accounts/{account_id}/quota")
    finally:
        app_instance.dependency_overrides.pop(get_proxy_context, None)

    assert response.status_code == 200
    account = response.json()["account"]
    assert account["accountId"] == account_id
    assert account["primaryWindow"]["remainingPercent"] == 75.0
    assert account["primaryWindow"]["resetAt"] is not None
    assert account["primaryWindow"]["windowMinutes"] == 300
    assert account["secondaryWindow"]["remainingPercent"] == 60.0
    assert account["secondaryWindow"]["resetAt"] is not None
    assert account["secondaryWindow"]["windowMinutes"] == 10080
    assert account["runtime"]["occupied"] is True
    assert account["runtime"]["sessions"] == 1
    assert account["runtime"]["pendingRequests"] == 1


@pytest.mark.asyncio
async def test_available_accounts_endpoint_returns_quota_status_list(async_client, app_instance):
    occupied_id = await _import_test_account(
        async_client,
        raw_account_id="acc_available_occupied",
        email="available-occupied@example.com",
    )
    free_id = await _import_test_account(
        async_client,
        raw_account_id="acc_available_free",
        email="available-free@example.com",
    )
    await _seed_usage_history(occupied_id, window="primary", used_percent=5.0, window_minutes=300)
    await _seed_usage_history(free_id, window="primary", used_percent=20.0, window_minutes=300)

    async def _fake_proxy_context() -> ProxyContext:
        return ProxyContext(
            service=_FakeProxyService(
                {
                    occupied_id: _FakeAccountRuntimeSnapshot(sessions=1),
                }
            )
        )

    app_instance.dependency_overrides[get_proxy_context] = _fake_proxy_context
    try:
        response = await async_client.get("/api/accounts/available")
    finally:
        app_instance.dependency_overrides.pop(get_proxy_context, None)

    assert response.status_code == 200
    accounts = {account["accountId"]: account for account in response.json()["accounts"]}
    assert occupied_id in accounts
    assert free_id in accounts
    assert accounts[occupied_id]["runtime"]["occupied"] is True
    assert accounts[occupied_id]["runtime"]["sessions"] == 1
    assert accounts[free_id]["runtime"]["occupied"] is False

    quota_list_response = await async_client.get("/api/accounts/quota")
    assert quota_list_response.status_code == 200
    assert "accounts" in quota_list_response.json()


@pytest.mark.asyncio
async def test_update_account_fast_service_tier_toggle(async_client):
    email = "fast-account@example.com"
    raw_account_id = "acc_fast_account"
    payload = {
        "email": email,
        "chatgpt_account_id": raw_account_id,
        "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"},
    }
    auth_json = {
        "tokens": {
            "idToken": _encode_jwt(payload),
            "accessToken": "access",
            "refreshToken": "refresh",
            "accountId": raw_account_id,
        },
    }

    expected_account_id = generate_unique_account_id(raw_account_id, email)
    files = {"auth_json": ("auth.json", json.dumps(auth_json), "application/json")}
    response = await async_client.post("/api/accounts/import", files=files)
    assert response.status_code == 200

    updated = await async_client.patch(
        f"/api/accounts/{expected_account_id}",
        json={
            "configuredPriority": 100,
            "fastServiceTierEnabled": True,
        },
    )

    assert updated.status_code == 200
    assert updated.json()["fastServiceTierEnabled"] is True

    list_response = await async_client.get("/api/accounts")
    assert list_response.status_code == 200
    account = next(
        account for account in list_response.json()["accounts"] if account["accountId"] == expected_account_id
    )
    assert account["fastServiceTierEnabled"] is True


@pytest.mark.asyncio
async def test_update_account_subscription_renews_at(async_client):
    email = "renewal-account@example.com"
    raw_account_id = "acc_subscription_renewal"
    account_id = await _import_test_account(
        async_client,
        raw_account_id=raw_account_id,
        email=email,
    )
    renews_at = datetime(2026, 7, 18, 20, 0, tzinfo=timezone(timedelta(hours=8))).isoformat()
    expected_renews_at = "2026-07-18T12:00:00Z"

    updated = await async_client.patch(
        f"/api/accounts/{account_id}",
        json={
            "configuredPriority": 100,
            "subscriptionRenewsAt": renews_at,
        },
    )

    assert updated.status_code == 200
    assert updated.json()["subscriptionRenewsAt"] == expected_renews_at

    list_response = await async_client.get("/api/accounts")
    assert list_response.status_code == 200
    account = next(account for account in list_response.json()["accounts"] if account["accountId"] == account_id)
    assert account["subscriptionRenewsAt"] == expected_renews_at

    cleared = await async_client.patch(
        f"/api/accounts/{account_id}",
        json={
            "configuredPriority": 100,
            "subscriptionRenewsAt": None,
        },
    )

    assert cleared.status_code == 200
    assert cleared.json()["subscriptionRenewsAt"] is None


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_update_all_accounts_fast_service_tier(async_client):
    for raw_account_id, email in (
        ("acc_fast_all_a", "fast-all-a@example.com"),
        ("acc_fast_all_b", "fast-all-b@example.com"),
    ):
        payload = {
            "email": email,
            "chatgpt_account_id": raw_account_id,
            "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"},
        }
        auth_json = {
            "tokens": {
                "idToken": _encode_jwt(payload),
                "accessToken": f"access-{raw_account_id}",
                "refreshToken": f"refresh-{raw_account_id}",
                "accountId": raw_account_id,
            },
        }
        files = {"auth_json": ("auth.json", json.dumps(auth_json), "application/json")}
        response = await async_client.post("/api/accounts/import", files=files)
        assert response.status_code == 200

    enabled = await async_client.post("/api/accounts/fast-service-tier", json={"enabled": True})
    assert enabled.status_code == 200
    assert enabled.json() == {"enabled": True, "updatedCount": 2}

    list_response = await async_client.get("/api/accounts")
    assert list_response.status_code == 200
    assert all(account["fastServiceTierEnabled"] is True for account in list_response.json()["accounts"])

    disabled = await async_client.post("/api/accounts/fast-service-tier", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json() == {"enabled": False, "updatedCount": 2}

    list_response = await async_client.get("/api/accounts")
    assert list_response.status_code == 200
    assert all(account["fastServiceTierEnabled"] is False for account in list_response.json()["accounts"])


@pytest.mark.asyncio
async def test_reactivate_missing_account_returns_404(async_client):
    response = await async_client.post("/api/accounts/missing/reactivate")
    assert response.status_code == 404
    payload = response.json()
    assert payload["error"]["code"] == "account_not_found"


@pytest.mark.asyncio
async def test_pause_missing_account_returns_404(async_client):
    response = await async_client.post("/api/accounts/missing/pause")
    assert response.status_code == 404
    payload = response.json()
    assert payload["error"]["code"] == "account_not_found"


@pytest.mark.asyncio
async def test_pause_account(async_client):
    email = "pause@example.com"
    raw_account_id = "acc_pause"
    payload = {
        "email": email,
        "chatgpt_account_id": raw_account_id,
        "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"},
    }
    auth_json = {
        "tokens": {
            "idToken": _encode_jwt(payload),
            "accessToken": "access",
            "refreshToken": "refresh",
            "accountId": raw_account_id,
        },
    }

    expected_account_id = generate_unique_account_id(raw_account_id, email)
    files = {"auth_json": ("auth.json", json.dumps(auth_json), "application/json")}
    response = await async_client.post("/api/accounts/import", files=files)
    assert response.status_code == 200

    pause = await async_client.post(f"/api/accounts/{expected_account_id}/pause")
    assert pause.status_code == 200
    assert pause.json()["status"] == "paused"

    accounts = await async_client.get("/api/accounts")
    assert accounts.status_code == 200
    data = accounts.json()["accounts"]
    matched = next((account for account in data if account["accountId"] == expected_account_id), None)
    assert matched is not None
    assert matched["status"] == "paused"


@pytest.mark.asyncio
async def test_delete_missing_account_returns_404(async_client):
    response = await async_client.delete("/api/accounts/missing")
    assert response.status_code == 404
    payload = response.json()
    assert payload["error"]["code"] == "account_not_found"
