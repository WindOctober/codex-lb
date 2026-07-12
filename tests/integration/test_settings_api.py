from __future__ import annotations

import base64
import json

import pytest

from app.core.auth import generate_unique_account_id

pytestmark = pytest.mark.integration


def _encode_jwt(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return f"header.{body}.sig"


@pytest.mark.asyncio
async def test_settings_api_get_and_update(async_client):
    response = await async_client.get("/api/settings")
    assert response.status_code == 200
    payload = response.json()
    assert payload["stickyThreadsEnabled"] is True
    assert payload["upstreamStreamTransport"] == "default"
    assert payload["preferEarlierResetAccounts"] is True
    assert payload["routingStrategy"] == "high_waterline"
    assert payload["openaiCacheAffinityMaxAgeSeconds"] == 1800
    assert payload["httpResponsesSessionBridgePromptCacheIdleTtlSeconds"] == 3600
    assert payload["httpResponsesSessionBridgeGatewaySafeMode"] is False
    assert payload["stickyReallocationBudgetThresholdPct"] == 95.0
    assert payload["importWithoutOverwrite"] is True
    assert payload["totpRequiredOnLogin"] is False
    assert payload["totpConfigured"] is False
    assert payload["apiKeyAuthEnabled"] is False
    assert payload["newsRefreshEnabled"] is False
    assert payload["scholarRefreshEnabled"] is False

    response = await async_client.put(
        "/api/settings",
        json={
            "stickyThreadsEnabled": False,
            "upstreamStreamTransport": "websocket",
            "preferEarlierResetAccounts": False,
            "routingStrategy": "primary_drain",
            "openaiCacheAffinityMaxAgeSeconds": 180,
            "httpResponsesSessionBridgePromptCacheIdleTtlSeconds": 1800,
            "httpResponsesSessionBridgeGatewaySafeMode": True,
            "stickyReallocationBudgetThresholdPct": 90.0,
            "importWithoutOverwrite": False,
            "totpRequiredOnLogin": False,
            "apiKeyAuthEnabled": True,
        },
    )
    assert response.status_code == 200
    updated = response.json()
    assert updated["stickyThreadsEnabled"] is False
    assert updated["upstreamStreamTransport"] == "websocket"
    assert updated["preferEarlierResetAccounts"] is False
    assert updated["routingStrategy"] == "primary_drain"
    assert updated["openaiCacheAffinityMaxAgeSeconds"] == 180
    assert updated["httpResponsesSessionBridgePromptCacheIdleTtlSeconds"] == 1800
    assert updated["httpResponsesSessionBridgeGatewaySafeMode"] is True
    assert updated["stickyReallocationBudgetThresholdPct"] == 90.0
    assert updated["importWithoutOverwrite"] is False
    assert updated["totpRequiredOnLogin"] is False
    assert updated["totpConfigured"] is False
    assert updated["apiKeyAuthEnabled"] is True
    assert updated["newsRefreshEnabled"] is False
    assert updated["scholarRefreshEnabled"] is False

    response = await async_client.get("/api/settings")
    assert response.status_code == 200
    payload = response.json()
    assert payload["stickyThreadsEnabled"] is False
    assert payload["upstreamStreamTransport"] == "websocket"
    assert payload["preferEarlierResetAccounts"] is False
    assert payload["routingStrategy"] == "primary_drain"
    assert payload["openaiCacheAffinityMaxAgeSeconds"] == 180
    assert payload["httpResponsesSessionBridgePromptCacheIdleTtlSeconds"] == 1800
    assert payload["httpResponsesSessionBridgeGatewaySafeMode"] is True
    assert payload["stickyReallocationBudgetThresholdPct"] == 90.0
    assert payload["importWithoutOverwrite"] is False
    assert payload["totpRequiredOnLogin"] is False
    assert payload["totpConfigured"] is False
    assert payload["apiKeyAuthEnabled"] is True


@pytest.mark.asyncio
async def test_settings_api_preserves_starred_priority_when_routing_leaves_drain(async_client):
    email = "primary-drain-priority@example.com"
    raw_account_id = "acc_primary_drain_priority"
    auth_json = {
        "tokens": {
            "idToken": _encode_jwt(
                {
                    "email": email,
                    "chatgpt_account_id": raw_account_id,
                    "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"},
                }
            ),
            "accessToken": "access",
            "refreshToken": "refresh",
            "accountId": raw_account_id,
        },
    }
    expected_account_id = generate_unique_account_id(raw_account_id, email)
    files = {"auth_json": ("auth.json", json.dumps(auth_json), "application/json")}
    imported = await async_client.post("/api/accounts/import", files=files)
    assert imported.status_code == 200

    enabled = await async_client.patch(
        f"/api/accounts/{expected_account_id}",
        json={"configuredPriority": 100, "primaryDrainPriorityEnabled": True},
    )
    assert enabled.status_code == 200
    assert enabled.json()["primaryDrainPriorityEnabled"] is True

    current = (await async_client.get("/api/settings")).json()
    switched = await async_client.put(
        "/api/settings",
        json={
            "stickyThreadsEnabled": current["stickyThreadsEnabled"],
            "upstreamStreamTransport": current["upstreamStreamTransport"],
            "preferEarlierResetAccounts": current["preferEarlierResetAccounts"],
            "routingStrategy": "capacity_weighted",
            "openaiCacheAffinityMaxAgeSeconds": current["openaiCacheAffinityMaxAgeSeconds"],
            "httpResponsesSessionBridgePromptCacheIdleTtlSeconds": current[
                "httpResponsesSessionBridgePromptCacheIdleTtlSeconds"
            ],
            "httpResponsesSessionBridgeGatewaySafeMode": current["httpResponsesSessionBridgeGatewaySafeMode"],
            "stickyReallocationBudgetThresholdPct": current["stickyReallocationBudgetThresholdPct"],
            "kycRoutingEnforcementEnabled": current["kycRoutingEnforcementEnabled"],
            "importWithoutOverwrite": current["importWithoutOverwrite"],
            "totpRequiredOnLogin": current["totpRequiredOnLogin"],
            "apiKeyAuthEnabled": current["apiKeyAuthEnabled"],
        },
    )
    assert switched.status_code == 200
    assert switched.json()["routingStrategy"] == "capacity_weighted"

    accounts = (await async_client.get("/api/accounts")).json()["accounts"]
    account = next(item for item in accounts if item["accountId"] == expected_account_id)
    assert account["primaryDrainPriorityEnabled"] is True
