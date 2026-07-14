from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import cast

import aiohttp
import pytest

import app.modules.proxy.service as proxy_module
from app.core.auth.refresh import RefreshError
from app.core.clients.proxy import ProxyResponseError
from app.core.config.settings import get_settings
from app.core.errors import openai_error
from app.core.openai.codex_search import CodexSearchResponse
from app.core.types import JsonObject
from app.modules.proxy._service.affinity import _prompt_cache_affinity_key_for_values

pytestmark = pytest.mark.integration


def _encode_jwt(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return f"header.{body}.sig"


def _make_auth_json(account_id: str, email: str) -> dict:
    payload = {
        "email": email,
        "chatgpt_account_id": account_id,
        "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"},
    }
    return {
        "tokens": {
            "idToken": _encode_jwt(payload),
            "accessToken": "access-token",
            "refreshToken": "refresh-token",
            "accountId": account_id,
        },
    }


async def _import_account(async_client, account_id: str, email: str) -> None:
    auth_json = _make_auth_json(account_id, email)
    files = {"auth_json": ("auth.json", json.dumps(auth_json), "application/json")}
    response = await async_client.post("/api/accounts/import", files=files)
    assert response.status_code == 200


def _completed_event(response_id: str) -> str:
    return 'data: {"type":"response.completed","response":{"id":"' + response_id + '","status":"completed"}}\n\n'


@pytest.mark.asyncio
async def test_v1_responses_forwards_input_file_url(async_client, monkeypatch):
    await _import_account(async_client, "acc_file_url", "file-url@example.com")

    seen = {}

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen["payload"] = payload
        yield _completed_event("resp_file_url")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {
        "model": "gpt-5.2",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Summarize this file."},
                    {"type": "input_file", "file_url": "https://example.com/file.pdf"},
                ],
            }
        ],
    }
    resp = await async_client.post("/v1/responses", json=payload)
    assert resp.status_code == 200
    assert seen["payload"].input == payload["input"]


@pytest.mark.asyncio
async def test_v1_responses_rejects_input_file_id(async_client):
    payload = {
        "model": "gpt-5.2",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Summarize this file."},
                    {"type": "input_file", "file_id": "file-123"},
                ],
            }
        ],
    }
    resp = await async_client.post("/v1/responses", json=payload)
    assert resp.status_code == 400
    payload = resp.json()
    assert payload["error"]["type"] == "invalid_request_error"
    assert payload["error"]["message"] == "Invalid request payload"
    assert payload["error"]["param"] == "input"


@pytest.mark.asyncio
async def test_v1_responses_accepts_previous_response_id(async_client, monkeypatch):
    await _import_account(async_client, "acc_prev_response_id", "prev-response-id@example.com")
    seen_previous_response_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False, **_kw):
        del headers, access_token, account_id, base_url, raise_for_status, _kw
        seen_previous_response_ids.append(getattr(payload, "previous_response_id", None))
        yield 'data: {"type":"response.completed","response":{"id":"resp_abc123"}}\n\n'

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {
        "model": "gpt-5.2",
        "previous_response_id": "resp_abc123",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "Continue."}],
            }
        ],
        "stream": True,
    }
    resp = await async_client.post("/v1/responses", json=payload)
    assert resp.status_code == 200
    assert seen_previous_response_ids == ["resp_abc123"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_payload",
    [
        {"type": "file_search", "vector_store_ids": ["vs_dummy"]},
        {"type": "code_interpreter", "container": {"type": "auto"}},
        {
            "type": "computer_use_preview",
            "display_width": 1024,
            "display_height": 768,
            "environment": "browser",
        },
        {"type": "image_generation"},
    ],
)
async def test_v1_responses_forwards_builtin_tools(async_client, monkeypatch, tool_payload):
    await _import_account(async_client, "acc_builtin_tools", "builtin-tools@example.com")

    seen = {}

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        del headers, access_token, account_id, base_url, raise_for_status
        seen["payload"] = payload
        yield _completed_event("resp_builtin_tools")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    request_payload = {
        "model": "gpt-5.2",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "Run tool."}],
            }
        ],
        "tools": [tool_payload],
    }

    resp = await async_client.post("/v1/responses", json=request_payload)
    assert resp.status_code == 200
    assert seen["payload"].tools == [tool_payload]


@pytest.mark.asyncio
async def test_v1_responses_forwards_input_string(async_client, monkeypatch):
    await _import_account(async_client, "acc_input_string", "input-string@example.com")

    seen = {}

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen["payload"] = payload
        yield _completed_event("resp_input_string")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.2", "input": "Hello"}
    resp = await async_client.post("/v1/responses", json=payload)
    assert resp.status_code == 200
    assert seen["payload"].input == [
        {"role": "user", "content": [{"type": "input_text", "text": "Hello"}]},
    ]


@pytest.mark.asyncio
async def test_v1_responses_forwards_include_logprobs(async_client, monkeypatch):
    await _import_account(async_client, "acc_include_logprobs", "include-logprobs@example.com")

    seen = {}

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen["payload"] = payload
        yield _completed_event("resp_include")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {
        "model": "gpt-5.2",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        "include": ["message.output_text.logprobs"],
    }
    resp = await async_client.post("/v1/responses", json=payload)
    assert resp.status_code == 200
    assert seen["payload"].include == ["message.output_text.logprobs"]


@pytest.mark.asyncio
async def test_v1_responses_preserves_prompt_cache_controls(async_client, monkeypatch):
    await _import_account(async_client, "acc_prompt_cache_v1", "prompt-cache-v1@example.com")

    seen = {}

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen["payload"] = payload.to_payload()
        yield _completed_event("resp_prompt_cache_v1")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {
        "model": "gpt-5.2",
        "input": "cache me",
        "prompt_cache_key": "thread_123",
        "prompt_cache_retention": "4h",
    }
    resp = await async_client.post("/v1/responses", json=payload)
    assert resp.status_code == 200
    assert seen["payload"]["prompt_cache_key"] == "thread_123"
    assert "prompt_cache_retention" not in seen["payload"]


@pytest.mark.asyncio
async def test_v1_responses_cache_controls_without_capable_provider_return_503(async_client) -> None:
    await _import_account(
        async_client,
        "acc_prompt_cache_capability_unavailable",
        "prompt-cache-capability-unavailable@example.com",
    )

    response = await async_client.post(
        "/v1/responses",
        json={
            "model": "gpt-5.6-sol",
            "input": "explicit cache request",
            "prompt_cache_options": {"mode": "explicit"},
            "stream": False,
        },
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "upstream_capability_unavailable"


@pytest.mark.asyncio
async def test_v1_responses_executes_and_preserves_gpt56_explicit_cache_requests(async_client, monkeypatch):
    await _import_account(async_client, "acc_prompt_cache_explicit", "prompt-cache-explicit@example.com")
    monkeypatch.setattr(
        "app.modules.proxy.load_balancer._account_supports_required_upstream_wire_api",
        lambda _account, _required: True,
    )

    seen_payloads: list[JsonObject] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False, **_kw):
        del headers, access_token, account_id, base_url, raise_for_status, _kw
        seen_payloads.append(payload.to_payload())
        request_number = len(seen_payloads)
        yield (
            "data: "
            + json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "id": f"resp_prompt_cache_{request_number}",
                        "status": "completed",
                        "model": "gpt-5.6-sol",
                        "output": [],
                        "usage": {
                            "input_tokens": 3072,
                            "input_tokens_details": {
                                "cached_tokens": 1024 if request_number == 2 else 0,
                                "cache_write_tokens": 2048 if request_number == 1 else 0,
                            },
                            "output_tokens": 1,
                            "total_tokens": 3073,
                        },
                    },
                }
            )
            + "\n\n"
        )

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    stable_block = {
        "type": "input_text",
        "text": "stable system and skill prefix",
        "prompt_cache_breakpoint": {"mode": "explicit"},
    }

    async def send(dynamic_text: str):
        return await async_client.post(
            "/v1/responses",
            json={
                "model": "gpt-5.6-sol",
                "prompt_cache_key": "semia:rules-v3:shard-03",
                "prompt_cache_options": {"mode": "explicit", "ttl": "30m"},
                "input": [
                    {"role": "system", "content": [stable_block]},
                    {"role": "user", "content": [{"type": "input_text", "text": dynamic_text}]},
                ],
                "stream": False,
            },
        )

    first = await send("dynamic iteration one")
    second = await send("dynamic iteration two")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == "resp_prompt_cache_1"
    assert second.json()["id"] == "resp_prompt_cache_2"
    assert first.json()["usage"]["input_tokens_details"] == {
        "cached_tokens": 0,
        "cache_write_tokens": 2048,
    }
    assert second.json()["usage"]["input_tokens_details"] == {
        "cached_tokens": 1024,
        "cache_write_tokens": 0,
    }
    assert len(seen_payloads) == 2
    assert [payload["prompt_cache_key"] for payload in seen_payloads] == [
        "semia:rules-v3:shard-03",
        "semia:rules-v3:shard-03",
    ]
    assert all(payload["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"} for payload in seen_payloads)
    assert seen_payloads[0]["input"] != seen_payloads[1]["input"]
    first_input = seen_payloads[0]["input"]
    assert isinstance(first_input, list)
    first_item = first_input[0]
    assert isinstance(first_item, dict)
    first_content = first_item["content"]
    assert isinstance(first_content, list)
    assert first_content[0] == stable_block


@pytest.mark.asyncio
async def test_v1_responses_applies_enforced_model_before_prompt_cache_model_gate(async_client, monkeypatch):
    enabled = await async_client.put(
        "/api/settings",
        json={
            "stickyThreadsEnabled": False,
            "preferEarlierResetAccounts": False,
            "totpRequiredOnLogin": False,
            "apiKeyAuthEnabled": True,
        },
    )
    assert enabled.status_code == 200
    created = await async_client.post(
        "/api/api-keys/",
        json={
            "name": "prompt-cache-enforced-gpt56",
            "allowedModels": ["gpt-5.6-sol"],
            "enforcedModel": "gpt-5.6-sol",
        },
    )
    assert created.status_code == 200
    api_key = created.json()["key"]
    await _import_account(async_client, "acc_prompt_cache_enforced", "prompt-cache-enforced@example.com")
    monkeypatch.setattr(
        "app.modules.proxy.load_balancer._account_supports_required_upstream_wire_api",
        lambda _account, _required: True,
    )

    seen_payloads: list[JsonObject] = []

    async def fake_stream(payload, headers, access_token, account_id, **kwargs):
        del headers, access_token, account_id, kwargs
        seen_payloads.append(payload.to_payload())
        yield _completed_event("resp_prompt_cache_enforced")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)
    response = await async_client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-5.5",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "stable prefix",
                            "prompt_cache_breakpoint": {"mode": "explicit"},
                        }
                    ],
                }
            ],
            "prompt_cache_options": {"mode": "explicit", "ttl": "30m"},
            "stream": False,
        },
    )

    assert response.status_code == 200
    assert seen_payloads[0]["model"] == "gpt-5.6-sol"
    assert seen_payloads[0]["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}


@pytest.mark.asyncio
async def test_v1_responses_stream_preserves_cache_write_usage(async_client, monkeypatch):
    await _import_account(async_client, "acc_prompt_cache_stream", "prompt-cache-stream@example.com")
    monkeypatch.setattr(
        "app.modules.proxy.load_balancer._account_supports_required_upstream_wire_api",
        lambda _account, _required: True,
    )

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False, **_kw):
        del payload, headers, access_token, account_id, base_url, raise_for_status, _kw
        yield (
            'data: {"type":"response.completed","response":{"id":"resp_cache_stream",'
            '"status":"completed","usage":{"input_tokens":2048,"input_tokens_details":'
            '{"cached_tokens":1024,"cache_write_tokens":2048},"output_tokens":1,"total_tokens":2049}}}\n\n'
        )

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    response = await async_client.post(
        "/v1/responses",
        json={
            "model": "gpt-5.6-sol",
            "input": "stream cache usage",
            "prompt_cache_options": {"mode": "explicit"},
            "stream": True,
        },
    )

    assert response.status_code == 200
    assert '"cached_tokens":1024' in response.text
    assert '"cache_write_tokens":2048' in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_payload",
    [
        {
            "model": "gpt-5.5",
            "input": "older model",
            "prompt_cache_options": {"mode": "explicit"},
        },
        {
            "model": "gpt-5.6-sol",
            "input": "invalid mode",
            "prompt_cache_options": {"mode": "automatic"},
        },
        {
            "model": "gpt-5.6-sol",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "unsupported block",
                            "prompt_cache_breakpoint": {"mode": "explicit"},
                        }
                    ],
                }
            ],
        },
    ],
)
async def test_v1_responses_rejects_invalid_or_incompatible_prompt_cache_controls(
    async_client,
    request_payload,
):
    response = await async_client.post("/v1/responses", json=request_payload)

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


@pytest.mark.asyncio
async def test_v1_responses_normalizes_prompt_cache_aliases(async_client, monkeypatch):
    await _import_account(async_client, "acc_prompt_cache_alias", "prompt-cache-alias@example.com")

    seen = {}

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen["payload"] = payload.to_payload()
        yield _completed_event("resp_prompt_cache_alias")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {
        "model": "gpt-5.2",
        "input": "cache me",
        "promptCacheKey": "thread_alias",
        "promptCacheRetention": "12h",
    }
    resp = await async_client.post("/v1/responses", json=payload)
    assert resp.status_code == 200
    assert seen["payload"]["prompt_cache_key"] == "thread_alias"
    assert "prompt_cache_retention" not in seen["payload"]
    assert "promptCacheKey" not in seen["payload"]
    assert "promptCacheRetention" not in seen["payload"]


@pytest.mark.asyncio
async def test_backend_responses_forwards_service_tier(async_client, monkeypatch):
    await _import_account(async_client, "acc_backend_service_tier", "backend-service-tier@example.com")

    seen = {}

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen["payload"] = payload
        yield _completed_event("resp_backend_service_tier")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    request_payload = {
        "model": "gpt-5.2",
        "instructions": "",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "Fast"}]}],
        "service_tier": "priority",
    }
    resp = await async_client.post("/backend-api/codex/responses", json=request_payload)
    assert resp.status_code == 200
    assert seen["payload"].service_tier == "priority"


@pytest.mark.asyncio
async def test_backend_responses_normalizes_fast_service_tier_for_upstream(async_client, monkeypatch):
    await _import_account(async_client, "acc_backend_fast_tier", "backend-fast-tier@example.com")

    seen = {}

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen["payload"] = payload.to_payload()
        yield _completed_event("resp_backend_fast_tier")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    request_payload = {
        "model": "gpt-5.2",
        "instructions": "",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "Fast"}]}],
        "service_tier": "fast",
    }
    resp = await async_client.post("/backend-api/codex/responses", json=request_payload)
    assert resp.status_code == 200
    assert seen["payload"]["service_tier"] == "priority"


@pytest.mark.asyncio
async def test_v1_responses_rejects_invalid_include(async_client):
    payload = {"model": "gpt-5.2", "input": "hi", "include": ["not_allowed"]}
    resp = await async_client.post("/v1/responses", json=payload)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_v1_responses_coerces_store_true_to_false(async_client):
    """store=true should be silently coerced to false (not rejected) so the
    bridge path can later override it on the upstream payload."""
    payload = {"model": "gpt-5.2", "input": "hi", "store": True}
    resp = await async_client.post("/v1/responses", json=payload)
    # 503 means it passed validation (no 400) but there are no upstream accounts in test
    assert resp.status_code != 400


@pytest.mark.asyncio
@pytest.mark.parametrize("truncation", ["auto", "disabled"])
async def test_v1_responses_rejects_truncation(async_client, truncation):
    payload = {"model": "gpt-5.2", "input": "hi", "truncation": truncation}
    resp = await async_client.post("/v1/responses", json=payload)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_v1_responses_rejects_conversation_and_previous(async_client):
    payload = {
        "model": "gpt-5.2",
        "input": "hi",
        "conversation": "conv_1",
        "previous_response_id": "resp_1",
    }
    resp = await async_client.post("/v1/responses", json=payload)
    assert resp.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_type", ["web_search", "web_search_preview"])
async def test_v1_responses_allows_web_search(async_client, monkeypatch, tool_type):
    await _import_account(async_client, "acc_web_search", "web-search@example.com")

    seen = {}

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen["payload"] = payload
        yield _completed_event("resp_web_search")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    request_payload = {
        "model": "gpt-5.2",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "Search"}]}],
        "tools": [{"type": tool_type}],
    }
    resp = await async_client.post("/v1/responses", json=request_payload)
    assert resp.status_code == 200
    assert seen["payload"].tools == [{"type": "web_search"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_type", ["web_search", "web_search_preview"])
async def test_backend_responses_allows_web_search(async_client, monkeypatch, tool_type):
    await _import_account(async_client, "acc_backend_web_search", "backend-web-search@example.com")

    seen = {}

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen["payload"] = payload
        yield _completed_event("resp_backend_web_search")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    request_payload = {
        "model": "gpt-5.2",
        "instructions": "",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "Search"}]}],
        "tools": [{"type": tool_type}],
    }
    resp = await async_client.post("/backend-api/codex/responses", json=request_payload)
    assert resp.status_code == 200
    assert seen["payload"].tools == [{"type": "web_search"}]


@pytest.mark.asyncio
async def test_backend_codex_alpha_search_forwards_typed_request(async_client, monkeypatch):
    await _import_account(async_client, "acc_alpha_search", "alpha-search@example.com")
    seen: dict[str, object] = {}
    parent_account_id: str | None = None

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        del payload, headers, access_token, base_url, raise_for_status
        nonlocal parent_account_id
        parent_account_id = account_id
        yield _completed_event("resp_alpha_search_parent")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)
    parent_response = await async_client.post(
        "/backend-api/codex/responses",
        json={
            "model": "gpt-5.2",
            "prompt_cache_key": "thread-alpha-search",
            "input": "Prepare the parent turn.",
        },
    )
    assert parent_response.status_code == 200
    assert parent_account_id == "acc_alpha_search"

    async def fake_search(
        payload,
        headers,
        access_token,
        account_id,
        *,
        base_url=None,
        wire_api="codex",
        timeout_seconds,
    ):
        seen.update(
            payload=payload,
            headers=dict(headers),
            access_token=access_token,
            account_id=account_id,
            base_url=base_url,
            wire_api=wire_api,
            timeout_seconds=timeout_seconds,
        )
        return CodexSearchResponse(
            encrypted_output="ciphertext",
            output="search result",
            future_response_field=True,
        )

    monkeypatch.setattr(proxy_module, "core_search_codex", fake_search)
    request_payload = {
        "id": "thread-alpha-search",
        "model": "gpt-5.2",
        "input": "Find current OpenAI documentation.",
        "commands": {"search_query": [{"q": "OpenAI documentation"}]},
        "settings": {"external_web_access": True},
        "max_output_tokens": 1000,
    }

    response = await async_client.post(
        "/backend-api/codex/alpha/search",
        json=request_payload,
        headers={"Authorization": "Bearer inbound-lb-key", "x-codex-client-version": "test"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "encrypted_output": "ciphertext",
        "output": "search result",
        "future_response_field": True,
    }
    assert seen["payload"].id == "thread-alpha-search"
    assert seen["wire_api"] == "codex"
    assert seen["account_id"] == "acc_alpha_search"
    assert seen["account_id"] == parent_account_id
    assert seen["access_token"] == "access-token"
    assert "authorization" not in {key.lower() for key in seen["headers"]}
    assert 480.0 < cast(float, seen["timeout_seconds"]) <= 600.0
    expected_sticky_key = _prompt_cache_affinity_key_for_values(
        cache_key="thread-alpha-search",
        model="gpt-5.2",
        api_key=None,
    )
    assert expected_sticky_key is not None
    sticky = await async_client.get(
        "/api/sticky-sessions",
        params={"kind": "prompt_cache", "keyQuery": expected_sticky_key},
    )
    assert sticky.status_code == 200
    assert any(
        entry["key"] == expected_sticky_key and entry["kind"] == "prompt_cache"
        for entry in sticky.json()["entries"]
    )
    legacy_sticky = await async_client.get(
        "/api/sticky-sessions",
        params={"kind": "prompt_cache", "keyQuery": "thread-alpha-search"},
    )
    assert legacy_sticky.status_code == 200
    assert not any(entry["key"] == "thread-alpha-search" for entry in legacy_sticky.json()["entries"])


@pytest.mark.asyncio
async def test_backend_codex_alpha_search_surfaces_account_neutral_error(async_client, monkeypatch):
    await _import_account(async_client, "acc_alpha_search_error", "alpha-search-error@example.com")
    attempts = 0

    async def failing_search(*args, **kwargs):
        nonlocal attempts
        del args, kwargs
        attempts += 1
        raise ProxyResponseError(
            400,
            openai_error(
                "invalid_request_error",
                "Invalid search command",
                error_type="invalid_request_error",
            ),
        )

    monkeypatch.setattr(proxy_module, "core_search_codex", failing_search)
    response = await async_client.post(
        "/backend-api/codex/alpha/search",
        json={"id": "thread-alpha-search-error", "model": "gpt-5.2"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request_error"
    assert attempts == 1


@pytest.mark.asyncio
async def test_backend_codex_alpha_search_fails_over_to_another_account(async_client, monkeypatch):
    await _import_account(async_client, "acc_alpha_search_first", "alpha-search-first@example.com")
    await _import_account(async_client, "acc_alpha_search_second", "alpha-search-second@example.com")
    attempted_accounts: list[str | None] = []

    async def rate_limited_then_success(
        payload,
        headers,
        access_token,
        account_id,
        *,
        base_url=None,
        wire_api="codex",
        timeout_seconds,
    ):
        del payload, headers, access_token, base_url, wire_api, timeout_seconds
        attempted_accounts.append(account_id)
        if len(attempted_accounts) == 1:
            raise ProxyResponseError(
                429,
                openai_error(
                    "rate_limit_exceeded",
                    "Search account is rate limited",
                    error_type="rate_limit_error",
                ),
            )
        return CodexSearchResponse(output="fallback search result")

    monkeypatch.setattr(proxy_module, "core_search_codex", rate_limited_then_success)
    response = await async_client.post(
        "/backend-api/codex/alpha/search",
        json={"id": "thread-alpha-search-failover", "model": "gpt-5.2"},
    )

    assert response.status_code == 200
    assert response.json()["output"] == "fallback search result"
    assert len(attempted_accounts) == 2
    assert attempted_accounts[0] != attempted_accounts[1]


@pytest.mark.asyncio
async def test_backend_codex_alpha_search_timeout_leaves_budget_for_another_account(
    async_client,
    monkeypatch,
):
    await _import_account(async_client, "acc_alpha_search_timeout_first", "alpha-search-timeout-first@example.com")
    await _import_account(async_client, "acc_alpha_search_timeout_second", "alpha-search-timeout-second@example.com")
    attempted_accounts: list[str | None] = []
    attempt_timeouts: list[float] = []

    async def timeout_then_success(
        payload,
        headers,
        access_token,
        account_id,
        *,
        base_url=None,
        wire_api="codex",
        timeout_seconds,
    ):
        del payload, headers, access_token, base_url, wire_api
        attempted_accounts.append(account_id)
        attempt_timeouts.append(timeout_seconds)
        if len(attempted_accounts) == 1:
            raise ProxyResponseError(
                502,
                openai_error("upstream_unavailable", "Timeout on reading data from socket"),
            )
        return CodexSearchResponse(output="search recovered on another account")

    monkeypatch.setattr(proxy_module, "core_search_codex", timeout_then_success)
    response = await async_client.post(
        "/backend-api/codex/alpha/search",
        json={"id": "thread-alpha-search-timeout-failover", "model": "gpt-5.2"},
    )

    assert response.status_code == 200
    assert response.json()["output"] == "search recovered on another account"
    assert len(attempted_accounts) == 2
    assert attempted_accounts[0] != attempted_accounts[1]
    assert all(480.0 < timeout_seconds <= 600.0 for timeout_seconds in attempt_timeouts)


@pytest.mark.asyncio
async def test_backend_codex_alpha_search_sanitizes_local_transport_error(
    async_client,
    monkeypatch,
):
    await _import_account(async_client, "acc_alpha_search_sanitize", "alpha-search-sanitize@example.com")

    async def fail_with_internal_transport_detail(*args, **kwargs):
        del args, kwargs
        raise aiohttp.ClientConnectionError("connect to 10.0.0.9:8443 via /srv/internal.sock")

    monkeypatch.setattr(proxy_module, "core_search_codex", fail_with_internal_transport_detail)
    response = await async_client.post(
        "/backend-api/codex/alpha/search",
        json={"id": "thread-alpha-search-sanitize", "model": "gpt-5.2"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["message"] == "Codex alpha search upstream request failed"
    assert "10.0.0.9" not in response.text
    assert "internal.sock" not in response.text


@pytest.mark.asyncio
async def test_backend_codex_alpha_search_sanitizes_refresh_response_body(
    async_client,
    monkeypatch,
):
    await _import_account(async_client, "acc_alpha_search_refresh_body", "alpha-search-refresh-body@example.com")

    async def fail_refresh(self, account, *, force=False, timeout_seconds=None):
        del self, account, force, timeout_seconds
        raise RefreshError(
            "invalid_grant",
            "connect to 10.0.0.9:8443 via /srv/internal.sock",
            True,
        )

    monkeypatch.setattr(proxy_module.ProxyService, "_ensure_fresh_with_budget", fail_refresh)
    response = await async_client.post(
        "/backend-api/codex/alpha/search",
        json={"id": "thread-alpha-search-refresh-body", "model": "gpt-5.2"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"
    assert response.json()["error"]["message"] == "Codex alpha search credential refresh failed"
    assert "10.0.0.9" not in response.text
    assert "internal.sock" not in response.text


@pytest.mark.asyncio
async def test_backend_codex_alpha_search_enforces_attempt_deadline_before_failover(
    async_client,
    monkeypatch,
):
    await _import_account(async_client, "acc_alpha_search_slow_first", "alpha-search-slow-first@example.com")
    await _import_account(async_client, "acc_alpha_search_fast_second", "alpha-search-fast-second@example.com")
    monkeypatch.setenv("CODEX_LB_CODEX_SEARCH_REQUEST_BUDGET_SECONDS", "1")
    monkeypatch.setenv("CODEX_LB_CODEX_SEARCH_ACCOUNT_ATTEMPT_TIMEOUT_SECONDS", "0.1")
    get_settings.cache_clear()
    attempted_accounts: list[str | None] = []

    async def slow_then_success(
        payload,
        headers,
        access_token,
        account_id,
        *,
        base_url=None,
        wire_api="codex",
        timeout_seconds,
    ):
        del payload, headers, access_token, base_url, wire_api, timeout_seconds
        attempted_accounts.append(account_id)
        if len(attempted_accounts) == 1:
            await asyncio.sleep(1.0)
        return CodexSearchResponse(output="search recovered within the total budget")

    monkeypatch.setattr(proxy_module, "core_search_codex", slow_then_success)
    response = await async_client.post(
        "/backend-api/codex/alpha/search",
        json={"id": "thread-alpha-search-hard-attempt-timeout", "model": "gpt-5.2"},
    )

    assert response.status_code == 200
    assert response.json()["output"] == "search recovered within the total budget"
    assert len(attempted_accounts) == 2
    assert attempted_accounts[0] != attempted_accounts[1]


@pytest.mark.asyncio
async def test_backend_codex_alpha_search_hard_attempt_timeout_ignores_late_cancellation_suppression(
    async_client,
    monkeypatch,
):
    await _import_account(async_client, "acc_alpha_search_late_first", "alpha-search-late-first@example.com")
    await _import_account(async_client, "acc_alpha_search_late_second", "alpha-search-late-second@example.com")
    monkeypatch.setenv("CODEX_LB_CODEX_SEARCH_REQUEST_BUDGET_SECONDS", "1")
    monkeypatch.setenv("CODEX_LB_CODEX_SEARCH_ACCOUNT_ATTEMPT_TIMEOUT_SECONDS", "0.05")
    get_settings.cache_clear()
    cancellation_seen = asyncio.Event()
    allow_late_result = asyncio.Event()
    attempted_accounts: list[str | None] = []

    async def suppress_then_succeed(
        payload,
        headers,
        access_token,
        account_id,
        *,
        base_url=None,
        wire_api="codex",
        timeout_seconds,
    ):
        del payload, headers, access_token, base_url, wire_api, timeout_seconds
        attempted_accounts.append(account_id)
        if len(attempted_accounts) == 1:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await allow_late_result.wait()
            return CodexSearchResponse(output="late first-account result must be ignored")
        return CodexSearchResponse(output="second account won within the hard budget")

    monkeypatch.setattr(proxy_module, "core_search_codex", suppress_then_succeed)
    started_at = time.monotonic()
    response = await asyncio.wait_for(
        async_client.post(
            "/backend-api/codex/alpha/search",
            json={"id": "thread-alpha-search-late-first", "model": "gpt-5.2"},
        ),
        timeout=1.0,
    )

    assert response.status_code == 200
    assert response.json()["output"] == "second account won within the hard budget"
    assert time.monotonic() - started_at < 0.5
    assert cancellation_seen.is_set()
    assert len(attempted_accounts) == 2
    allow_late_result.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_backend_codex_alpha_search_blocked_failure_accounting_does_not_block_failover(
    async_client,
    monkeypatch,
):
    await _import_account(async_client, "acc_alpha_search_accounting", "alpha-search-accounting@example.com")
    await _import_account(async_client, "acc_alpha_search_accounting_2", "alpha-search-accounting-2@example.com")
    monkeypatch.setenv("CODEX_LB_CODEX_SEARCH_REQUEST_BUDGET_SECONDS", "0.2")
    monkeypatch.setenv("CODEX_LB_CODEX_SEARCH_ACCOUNT_ATTEMPT_TIMEOUT_SECONDS", "0.1")
    get_settings.cache_clear()
    accounting_started = asyncio.Event()
    allow_accounting = asyncio.Event()
    attempted_accounts: list[str | None] = []

    async def rate_limited(*args, **kwargs):
        del kwargs
        attempted_accounts.append(cast(str | None, args[3]))
        raise ProxyResponseError(
            429,
            openai_error("rate_limit_exceeded", "Search account is rate limited"),
        )

    async def blocked_accounting(*args, **kwargs):
        del args, kwargs
        accounting_started.set()
        await allow_accounting.wait()

    monkeypatch.setattr(proxy_module, "core_search_codex", rate_limited)
    monkeypatch.setattr(proxy_module.ProxyService, "_handle_proxy_error", blocked_accounting)

    response = await asyncio.wait_for(
        async_client.post(
            "/backend-api/codex/alpha/search",
            json={"id": "thread-alpha-search-accounting", "model": "gpt-5.2"},
        ),
        timeout=1.0,
    )

    assert accounting_started.is_set()
    assert response.status_code == 429
    assert len(attempted_accounts) == 2
    assert attempted_accounts[0] != attempted_accounts[1]
    allow_accounting.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_backend_codex_alpha_search_does_not_block_success_on_request_log_after_deadline(
    async_client,
    monkeypatch,
):
    await _import_account(async_client, "acc_alpha_search_log", "alpha-search-log@example.com")
    monkeypatch.setenv("CODEX_LB_CODEX_SEARCH_REQUEST_BUDGET_SECONDS", "0.2")
    monkeypatch.setenv("CODEX_LB_CODEX_SEARCH_ACCOUNT_ATTEMPT_TIMEOUT_SECONDS", "0.1")
    get_settings.cache_clear()
    log_started = asyncio.Event()
    allow_log = asyncio.Event()

    async def successful_search(*args, **kwargs):
        del args, kwargs
        return CodexSearchResponse(output="search completed before logging")

    async def blocked_log(*args, **kwargs):
        del args, kwargs
        log_started.set()
        await allow_log.wait()

    monkeypatch.setattr(proxy_module, "core_search_codex", successful_search)
    monkeypatch.setattr(proxy_module.ProxyService, "_write_request_log", blocked_log)

    started_at = time.monotonic()
    response = await asyncio.wait_for(
        async_client.post(
            "/backend-api/codex/alpha/search",
            json={"id": "thread-alpha-search-log", "model": "gpt-5.2"},
        ),
        timeout=1.0,
    )
    elapsed = time.monotonic() - started_at
    await asyncio.wait_for(log_started.wait(), timeout=1.0)
    allow_log.set()

    assert response.status_code == 200
    assert response.json()["output"] == "search completed before logging"
    assert elapsed < 0.15


@pytest.mark.asyncio
async def test_backend_codex_alpha_search_does_not_block_success_on_health_accounting(
    async_client,
    monkeypatch,
):
    await _import_account(async_client, "acc_alpha_search_success", "alpha-search-success@example.com")
    monkeypatch.setenv("CODEX_LB_CODEX_SEARCH_REQUEST_BUDGET_SECONDS", "0.2")
    monkeypatch.setenv("CODEX_LB_CODEX_SEARCH_ACCOUNT_ATTEMPT_TIMEOUT_SECONDS", "0.1")
    get_settings.cache_clear()
    accounting_started = asyncio.Event()
    allow_accounting = asyncio.Event()

    async def successful_search(*args, **kwargs):
        del args, kwargs
        return CodexSearchResponse(output="search completed before health accounting")

    async def blocked_success_accounting(*args, **kwargs):
        del args, kwargs
        accounting_started.set()
        await allow_accounting.wait()

    monkeypatch.setattr(proxy_module, "core_search_codex", successful_search)
    monkeypatch.setattr(proxy_module.LoadBalancer, "record_success", blocked_success_accounting)

    started_at = time.monotonic()
    response = await asyncio.wait_for(
        async_client.post(
            "/backend-api/codex/alpha/search",
            json={"id": "thread-alpha-search-success", "model": "gpt-5.2"},
        ),
        timeout=1.0,
    )
    elapsed = time.monotonic() - started_at
    await asyncio.wait_for(accounting_started.wait(), timeout=1.0)
    allow_accounting.set()

    assert response.status_code == 200
    assert response.json()["output"] == "search completed before health accounting"
    assert elapsed < 0.15


@pytest.mark.asyncio
async def test_backend_codex_alpha_search_forced_refresh_keeps_one_account_attempt_deadline(
    async_client,
    monkeypatch,
):
    await _import_account(async_client, "acc_alpha_search_refresh", "alpha-search-refresh@example.com")
    monkeypatch.setenv("CODEX_LB_CODEX_SEARCH_REQUEST_BUDGET_SECONDS", "2")
    monkeypatch.setenv("CODEX_LB_CODEX_SEARCH_ACCOUNT_ATTEMPT_TIMEOUT_SECONDS", "0.2")
    get_settings.cache_clear()

    attempt_timeouts: list[float] = []

    async def ensure_fresh(account, *, force=False, timeout_seconds=None):
        del timeout_seconds
        if force:
            await asyncio.sleep(0.05)
        return account

    async def unauthorized_then_success(
        payload,
        headers,
        access_token,
        account_id,
        *,
        base_url=None,
        wire_api="codex",
        timeout_seconds,
    ):
        del payload, headers, access_token, account_id, base_url, wire_api
        attempt_timeouts.append(timeout_seconds)
        if len(attempt_timeouts) == 1:
            raise ProxyResponseError(
                401,
                openai_error("invalid_api_key", "Refresh this account"),
            )
        return CodexSearchResponse(output="search recovered after refresh")

    monkeypatch.setattr(proxy_module.ProxyService, "_ensure_fresh_with_budget", staticmethod(ensure_fresh))
    monkeypatch.setattr(proxy_module, "core_search_codex", unauthorized_then_success)
    response = await async_client.post(
        "/backend-api/codex/alpha/search",
        json={"id": "thread-alpha-search-refresh", "model": "gpt-5.2"},
    )

    assert response.status_code == 200
    assert response.json()["output"] == "search recovered after refresh"
    assert len(attempt_timeouts) == 2
    assert 0.15 < attempt_timeouts[0] <= 0.2
    assert 0 < attempt_timeouts[1] < attempt_timeouts[0] - 0.04


@pytest.mark.asyncio
async def test_backend_codex_alpha_search_surfaces_final_transient_failure(async_client, monkeypatch):
    await _import_account(async_client, "acc_alpha_search_failure_first", "alpha-search-failure-first@example.com")
    await _import_account(async_client, "acc_alpha_search_failure_second", "alpha-search-failure-second@example.com")
    attempted_accounts: list[str | None] = []

    async def fail_both_accounts(
        payload,
        headers,
        access_token,
        account_id,
        *,
        base_url=None,
        wire_api="codex",
        timeout_seconds,
    ):
        del payload, headers, access_token, base_url, wire_api, timeout_seconds
        attempted_accounts.append(account_id)
        message = "first transient search failure" if len(attempted_accounts) == 1 else "final search timeout"
        raise ProxyResponseError(
            502,
            openai_error("upstream_unavailable", message),
        )

    monkeypatch.setattr(proxy_module, "core_search_codex", fail_both_accounts)
    response = await async_client.post(
        "/backend-api/codex/alpha/search",
        json={"id": "thread-alpha-search-final-failure", "model": "gpt-5.2"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_unavailable"
    assert response.json()["error"]["message"] == "final search timeout"
    assert len(attempted_accounts) == 2
    assert attempted_accounts[0] != attempted_accounts[1]


@pytest.mark.asyncio
async def test_backend_codex_alpha_search_enforces_auth_and_model_policy(async_client, monkeypatch):
    enable = await async_client.put(
        "/api/settings",
        json={
            "stickyThreadsEnabled": False,
            "preferEarlierResetAccounts": False,
            "totpRequiredOnLogin": False,
            "apiKeyAuthEnabled": True,
        },
    )
    assert enable.status_code == 200
    created = await async_client.post(
        "/api/api-keys/",
        json={"name": "alpha-search-restricted", "allowedModels": ["gpt-5.1"]},
    )
    assert created.status_code == 200
    api_key = created.json()["key"]
    upstream_called = False

    async def unexpected_search(*args, **kwargs):
        nonlocal upstream_called
        del args, kwargs
        upstream_called = True
        return CodexSearchResponse(output="unexpected")

    monkeypatch.setattr(proxy_module, "core_search_codex", unexpected_search)
    request_payload = {"id": "thread-alpha-search-policy", "model": "gpt-5.2"}

    unauthenticated = await async_client.post(
        "/backend-api/codex/alpha/search",
        json=request_payload,
    )
    blocked_model = await async_client.post(
        "/backend-api/codex/alpha/search",
        json=request_payload,
        headers={"Authorization": f"Bearer {api_key}"},
    )

    assert unauthenticated.status_code == 401
    assert blocked_model.status_code == 403
    assert blocked_model.json()["error"]["code"] == "model_not_allowed"
    assert upstream_called is False


@pytest.mark.asyncio
async def test_v1_chat_completions_rejects_non_text_developer(async_client):
    payload = {
        "model": "gpt-5.2",
        "messages": [
            {
                "role": "developer",
                "content": [{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}],
            },
            {"role": "user", "content": "hi"},
        ],
    }
    resp = await async_client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_v1_chat_completions_rejects_invalid_audio(async_client):
    payload = {
        "model": "gpt-5.2",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "input_audio", "input_audio": {"data": "AAA", "format": "ogg"}},
                ],
            }
        ],
    }
    resp = await async_client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_v1_chat_completions_maps_response_format(async_client, monkeypatch):
    await _import_account(async_client, "acc_chat_format", "chat-format@example.com")

    seen = {}

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen["payload"] = payload
        yield _completed_event("resp_chat_format")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {
        "model": "gpt-5.2",
        "messages": [{"role": "user", "content": "Return JSON."}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "result_schema",
                "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                "strict": True,
            },
        },
    }
    resp = await async_client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    text = seen["payload"].text
    assert text is not None
    assert text.format is not None
    assert text.format.type == "json_schema"
    assert text.format.name == "result_schema"


@pytest.mark.asyncio
async def test_v1_chat_completions_rejects_missing_json_schema(async_client):
    payload = {
        "model": "gpt-5.2",
        "messages": [{"role": "user", "content": "Return JSON."}],
        "response_format": {"type": "json_schema"},
    }
    resp = await async_client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_v1_chat_completions_forwards_multimodal(async_client, monkeypatch):
    await _import_account(async_client, "acc_chat_multi", "chat-multi@example.com")

    seen = {}

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen["payload"] = payload
        yield _completed_event("resp_chat_multi")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {
        "model": "gpt-5.2",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Check image and audio."},
                    {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
                    {"type": "file", "file": {"file_url": "https://example.com/file.pdf"}},
                ],
            }
        ],
    }
    resp = await async_client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    assert isinstance(seen["payload"].input, list)
    assert seen["payload"].input[0]["role"] == "user"
    content = seen["payload"].input[0]["content"]
    assert content[0] == {"type": "input_text", "text": "Check image and audio."}
    assert content[1] == {"type": "input_image", "image_url": "https://example.com/a.png"}
    assert content[2] == {"type": "input_file", "file_url": "https://example.com/file.pdf"}


@pytest.mark.asyncio
async def test_v1_chat_completions_rejects_file_id(async_client):
    payload = {
        "model": "gpt-5.2",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Summarize file."},
                    {"type": "file", "file": {"file_id": "file-123"}},
                ],
            }
        ],
    }
    resp = await async_client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 400
    payload = resp.json()
    assert payload["error"]["type"] == "invalid_request_error"
    assert payload["error"]["message"] == "Invalid request payload"
    assert payload["error"]["param"] == "messages"


@pytest.mark.asyncio
async def test_v1_chat_completions_rejects_audio_input(async_client):
    payload = {
        "model": "gpt-5.2",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Transcribe audio."},
                    {"type": "input_audio", "input_audio": {"data": "AAA", "format": "wav"}},
                ],
            }
        ],
    }
    resp = await async_client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_v1_chat_completions_allows_image_generation(async_client, monkeypatch):
    await _import_account(async_client, "acc_chat_image_generation", "chat-image-generation@example.com")

    seen = {}

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen["payload"] = payload
        yield _completed_event("resp_chat_image_generation")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {
        "model": "gpt-5.2",
        "messages": [{"role": "user", "content": "Generate an image."}],
        "tools": [{"type": "image_generation"}],
    }
    resp = await async_client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    assert seen["payload"].tools == [{"type": "image_generation"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_type", ["web_search", "web_search_preview"])
async def test_v1_chat_completions_allows_web_search(async_client, monkeypatch, tool_type):
    await _import_account(async_client, "acc_chat_web_search", "chat-web-search@example.com")

    seen = {}

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen["payload"] = payload
        yield _completed_event("resp_chat_web_search")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {
        "model": "gpt-5.2",
        "messages": [{"role": "user", "content": "Search the web."}],
        "tools": [{"type": tool_type}],
    }
    resp = await async_client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    assert seen["payload"].tools == [{"type": "web_search"}]


@pytest.mark.asyncio
async def test_v1_chat_completions_normalizes_tools_and_tool_choice(async_client, monkeypatch):
    await _import_account(async_client, "acc_chat_tools", "chat-tools@example.com")

    seen = {}

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen["payload"] = payload
        yield _completed_event("resp_chat_tools")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {
        "model": "gpt-5.2",
        "messages": [{"role": "user", "content": "Weather?"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather",
                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
    }
    resp = await async_client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    assert seen["payload"].tools == [
        {
            "type": "function",
            "name": "get_weather",
            "description": "Get weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        }
    ]
    assert seen["payload"].tool_choice == {"type": "function", "name": "get_weather"}


@pytest.mark.asyncio
async def test_v1_chat_completions_does_not_enable_codex_session_affinity(async_client, monkeypatch):
    await _import_account(async_client, "acc_chat_affinity_a", "chat-affinity-a@example.com")
    await _import_account(async_client, "acc_chat_affinity_b", "chat-affinity-b@example.com")

    seen = {}

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen["account_id"] = account_id
        seen["prompt_cache_key"] = getattr(payload, "prompt_cache_key", None)
        yield _completed_event("resp_chat_affinity")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {
        "model": "gpt-5.2",
        "messages": [{"role": "user", "content": "Weather?"}],
    }
    resp = await async_client.post("/v1/chat/completions", json=payload, headers={"session_id": "chat-session-123"})
    assert resp.status_code == 200
    assert isinstance(seen["prompt_cache_key"], str)
    assert seen["prompt_cache_key"]


@pytest.mark.asyncio
async def test_v1_chat_completions_maps_reasoning_effort(async_client, monkeypatch):
    await _import_account(async_client, "acc_chat_reason", "chat-reason@example.com")

    seen = {}

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen["payload"] = payload
        yield _completed_event("resp_chat_reason")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {
        "model": "gpt-5.2",
        "messages": [{"role": "user", "content": "Think."}],
        "reasoning_effort": "low",
    }
    resp = await async_client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    assert seen["payload"].reasoning is not None
    assert seen["payload"].reasoning.effort == "low"


@pytest.mark.asyncio
async def test_v1_chat_completions_forwards_service_tier(async_client, monkeypatch):
    await _import_account(async_client, "acc_chat_service_tier", "chat-service-tier@example.com")

    seen = {}

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen["payload"] = payload
        yield _completed_event("resp_chat_service_tier")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {
        "model": "gpt-5.2",
        "messages": [{"role": "user", "content": "Think fast."}],
        "service_tier": "priority",
    }
    resp = await async_client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    assert seen["payload"].service_tier == "priority"


@pytest.mark.asyncio
async def test_v1_chat_completions_preserves_prompt_cache_controls(async_client, monkeypatch):
    await _import_account(async_client, "acc_chat_prompt_cache", "chat-prompt-cache@example.com")

    seen = {}

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen["payload"] = payload.to_payload()
        yield _completed_event("resp_chat_prompt_cache")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {
        "model": "gpt-5.2",
        "messages": [{"role": "user", "content": "Cache this chat."}],
        "prompt_cache_key": "chat_thread_123",
        "prompt_cache_retention": "8h",
    }
    resp = await async_client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    assert seen["payload"]["prompt_cache_key"] == "chat_thread_123"
    assert "prompt_cache_retention" not in seen["payload"]
