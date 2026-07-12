from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

import app.modules.proxy.api as proxy_api_module

pytestmark = pytest.mark.unit


async def _iter_blocks(*blocks: str) -> AsyncIterator[str]:
    for block in blocks:
        yield block


@pytest.mark.asyncio
async def test_collect_responses_payload_returns_contract_error_on_truncated_stream() -> None:
    result = await proxy_api_module._collect_responses_payload(
        _iter_blocks('data: {"type":"response.output_text.delta","delta":"hello"}\n\n')
    )

    body = result.model_dump(mode="json", exclude_none=True)
    assert body["error"]["code"] == "upstream_stream_truncated"


@pytest.mark.asyncio
async def test_collect_responses_payload_normalizes_unknown_output_item_to_message() -> None:
    result = await proxy_api_module._collect_responses_payload(
        _iter_blocks(
            (
                'data: {"type":"response.output_item.done","output_index":0,'
                '"item":{"id":"fa_1","type":"final_answer","text":"hello from final answer"}}\n\n'
            ),
            (
                'data: {"type":"response.completed","response":{"id":"resp_1","object":"response",'
                '"status":"completed","output":[]}}\n\n'
            ),
        )
    )

    body = result.model_dump(mode="json", exclude_none=True)
    assert body["id"] == "resp_1"
    assert body["output"] == [
        {
            "id": "fa_1",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "hello from final answer"}],
        }
    ]


@pytest.mark.asyncio
async def test_normalize_public_responses_stream_appends_response_failed_on_invalid_json() -> None:
    blocks = [
        block
        async for block in proxy_api_module._normalize_public_responses_stream(_iter_blocks("data: {not-json}\n\n"))
    ]

    assert len(blocks) == 1
    payload = proxy_api_module._parse_sse_payload(blocks[0])
    assert payload is not None
    assert payload["type"] == "response.failed"
    response = payload["response"]
    assert isinstance(response, dict)
    error = response["error"]
    assert isinstance(error, dict)
    assert error["code"] == "invalid_json"


@pytest.mark.asyncio
async def test_normalize_public_responses_stream_normalizes_unknown_terminal_output_item() -> None:
    blocks = [
        block
        async for block in proxy_api_module._normalize_public_responses_stream(
            _iter_blocks(
                (
                    'data: {"type":"response.completed","response":{"id":"resp_1","object":"response",'
                    '"status":"completed","output":[{"id":"fa_1","type":"final_answer","text":"normalized"}]}}\n\n'
                )
            )
        )
    ]

    assert len(blocks) == 1
    payload = proxy_api_module._parse_sse_payload(blocks[0])
    assert payload is not None
    assert payload["type"] == "response.completed"
    response = payload["response"]
    assert isinstance(response, dict)
    output = response["output"]
    assert isinstance(output, list)
    assert output == [
        {
            "id": "fa_1",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "normalized"}],
        }
    ]


@pytest.mark.asyncio
async def test_normalize_public_responses_stream_preserves_remote_compaction_v2_output_item() -> None:
    blocks = [
        block
        async for block in proxy_api_module._normalize_public_responses_stream(
            _iter_blocks(
                (
                    'data: {"type":"response.output_item.done","output_index":0,'
                    '"item":{"id":"cc_1","type":"context_compaction","encrypted_content":"enc_1",'
                    '"summary":"opaque compact state"}}\n\n'
                ),
                (
                    'data: {"type":"response.completed","response":{"id":"resp_1","object":"response",'
                    '"status":"completed","output":[{"id":"cc_1","type":"context_compaction",'
                    '"encrypted_content":"enc_1","summary":"opaque compact state"}]}}\n\n'
                ),
            ),
            allow_remote_compaction_v2=True,
        )
    ]

    assert len(blocks) == 2
    item_payload = proxy_api_module._parse_sse_payload(blocks[0])
    assert item_payload is not None
    assert item_payload["type"] == "response.output_item.done"
    assert item_payload["item"] == {
        "id": "cc_1",
        "type": "context_compaction",
        "encrypted_content": "enc_1",
        "summary": "opaque compact state",
    }
    terminal_payload = proxy_api_module._parse_sse_payload(blocks[1])
    assert terminal_payload is not None
    response = terminal_payload["response"]
    assert isinstance(response, dict)
    assert response["output"] == [
        {
            "id": "cc_1",
            "type": "context_compaction",
            "encrypted_content": "enc_1",
            "summary": "opaque compact state",
        }
    ]


@pytest.mark.asyncio
async def test_normalize_public_responses_stream_merges_remote_compaction_v2_item_into_empty_terminal_output() -> None:
    blocks = [
        block
        async for block in proxy_api_module._normalize_public_responses_stream(
            _iter_blocks(
                (
                    'data: {"type":"response.output_item.done","output_index":0,'
                    '"item":{"id":"cc_1","type":"context_compaction","encrypted_content":"enc_1"}}\n\n'
                ),
                (
                    'data: {"type":"response.completed","response":{"id":"resp_1","object":"response",'
                    '"status":"completed","output":[]}}\n\n'
                ),
            ),
            allow_remote_compaction_v2=True,
        )
    ]

    assert len(blocks) == 2
    terminal_payload = proxy_api_module._parse_sse_payload(blocks[1])
    assert terminal_payload is not None
    response = terminal_payload["response"]
    assert isinstance(response, dict)
    assert response["output"] == [
        {
            "id": "cc_1",
            "type": "context_compaction",
            "encrypted_content": "enc_1",
        }
    ]


@pytest.mark.asyncio
async def test_normalize_public_responses_stream_prefers_remote_compaction_v2_item_over_terminal_placeholder() -> None:
    blocks = [
        block
        async for block in proxy_api_module._normalize_public_responses_stream(
            _iter_blocks(
                (
                    'data: {"type":"response.output_item.done","output_index":0,'
                    '"item":{"id":"cc_1","type":"context_compaction","encrypted_content":"enc_1"}}\n\n'
                ),
                (
                    'data: {"type":"response.completed","response":{"id":"resp_1","object":"response",'
                    '"status":"completed","output":[{"id":"opaque_1","type":"opaque_beta_state"}]}}\n\n'
                ),
            ),
            allow_remote_compaction_v2=True,
        )
    ]

    assert len(blocks) == 2
    terminal_payload = proxy_api_module._parse_sse_payload(blocks[1])
    assert terminal_payload is not None
    assert terminal_payload["type"] == "response.completed"
    response = terminal_payload["response"]
    assert isinstance(response, dict)
    assert response["output"] == [
        {
            "id": "cc_1",
            "type": "context_compaction",
            "encrypted_content": "enc_1",
        }
    ]


@pytest.mark.asyncio
async def test_normalize_public_responses_stream_codex_route_preserves_opaque_compaction_items() -> None:
    blocks = [
        block
        async for block in proxy_api_module._normalize_public_responses_stream(
            _iter_blocks(
                (
                    'data: {"type":"response.output_item.done","sequence_number":1,"output_index":0,'
                    '"item":{"id":"cc_1","type":"context_compaction","encrypted_content":"enc_1"}}\n\n'
                ),
                (
                    'data: {"type":"response.completed","sequence_number":2,'
                    '"response":{"id":"resp_1","object":"response","status":"completed",'
                    '"output":[{"id":"opaque_1","type":"opaque_beta_state"}]}}\n\n'
                ),
            ),
            enforce_openai_sdk_contract=False,
        )
    ]

    payloads = [proxy_api_module._parse_sse_payload(block) for block in blocks]
    event_types = [payload["type"] for payload in payloads if payload is not None]
    assert event_types == ["response.output_item.done", "response.completed"]
    assert blocks[-1] == "data: [DONE]\n\n"
    item_payload = payloads[0]
    assert item_payload is not None
    assert item_payload["item"] == {
        "id": "cc_1",
        "type": "context_compaction",
        "encrypted_content": "enc_1",
    }
    terminal_payload = payloads[1]
    assert terminal_payload is not None
    response = terminal_payload["response"]
    assert isinstance(response, dict)
    assert response["output"] == [{"id": "opaque_1", "type": "opaque_beta_state"}]


@pytest.mark.asyncio
async def test_normalize_public_responses_stream_drops_context_compaction_without_remote_compaction_v2() -> None:
    blocks = [
        block
        async for block in proxy_api_module._normalize_public_responses_stream(
            _iter_blocks(
                (
                    'data: {"type":"response.output_item.done","output_index":0,'
                    '"item":{"id":"cc_1","type":"context_compaction","encrypted_content":"enc_1"}}\n\n'
                ),
                (
                    'data: {"type":"response.completed","response":{"id":"resp_1","object":"response",'
                    '"status":"completed","output":[]}}\n\n'
                ),
            )
        )
    ]

    assert len(blocks) == 1
    terminal_payload = proxy_api_module._parse_sse_payload(blocks[0])
    assert terminal_payload is not None
    assert terminal_payload["type"] == "response.completed"
    response = terminal_payload["response"]
    assert isinstance(response, dict)
    assert response["output"] == []


@pytest.mark.asyncio
async def test_normalize_public_responses_stream_rejects_terminal_unknown_item_without_text() -> None:
    blocks = [
        block
        async for block in proxy_api_module._normalize_public_responses_stream(
            _iter_blocks(
                (
                    'data: {"type":"response.completed","response":{"id":"resp_1","object":"response",'
                    '"status":"completed","output":[{"id":"opaque_1","type":"opaque_beta_state"}]}}\n\n'
                )
            )
        )
    ]

    assert len(blocks) == 1
    terminal_payload = proxy_api_module._parse_sse_payload(blocks[0])
    assert terminal_payload is not None
    assert terminal_payload["type"] == "response.failed"
    response = terminal_payload["response"]
    assert isinstance(response, dict)
    error = response["error"]
    assert isinstance(error, dict)
    assert error["code"] == "invalid_output_item"


@pytest.mark.asyncio
async def test_collect_responses_payload_preserves_remote_compaction_v2_output_item() -> None:
    result = await proxy_api_module._collect_responses_payload(
        _iter_blocks(
            (
                'data: {"type":"response.output_item.done","output_index":0,'
                '"item":{"id":"cc_2","type":"context_compaction","encrypted_content":"enc_2"}}\n\n'
            ),
            (
                'data: {"type":"response.completed","response":{"id":"resp_2","object":"response",'
                '"status":"completed","output":[]}}\n\n'
            ),
        ),
        allow_remote_compaction_v2=True,
    )

    body = result.model_dump(mode="json", exclude_none=True)
    assert body["id"] == "resp_2"
    assert body["output"] == [
        {
            "id": "cc_2",
            "type": "context_compaction",
            "encrypted_content": "enc_2",
        }
    ]


@pytest.mark.asyncio
async def test_collect_responses_payload_preserves_apply_patch_call_output_item() -> None:
    result = await proxy_api_module._collect_responses_payload(
        _iter_blocks(
            (
                'data: {"type":"response.output_item.done","output_index":0,'
                '"item":{"id":"apc_1","type":"apply_patch_call","status":"completed",'
                '"call_id":"call_1","patch":"*** Begin Patch\\n*** End Patch\\n"}}\n\n'
            ),
            (
                'data: {"type":"response.completed","response":{"id":"resp_1","object":"response",'
                '"status":"completed","output":[]}}\n\n'
            ),
        )
    )

    body = result.model_dump(mode="json", exclude_none=True)
    assert body["id"] == "resp_1"
    assert body["output"] == [
        {
            "id": "apc_1",
            "type": "apply_patch_call",
            "status": "completed",
            "call_id": "call_1",
            "patch": "*** Begin Patch\n*** End Patch\n",
        }
    ]


@pytest.mark.asyncio
async def test_collect_responses_payload_preserves_mcp_approval_request_output_item() -> None:
    result = await proxy_api_module._collect_responses_payload(
        _iter_blocks(
            (
                'data: {"type":"response.output_item.done","output_index":0,'
                '"item":{"id":"mcp_1","type":"mcp_approval_request","status":"in_progress",'
                '"request_id":"req_1","server_label":"github","tool_name":"repos/list"}}\n\n'
            ),
            (
                'data: {"type":"response.completed","response":{"id":"resp_2","object":"response",'
                '"status":"completed","output":[]}}\n\n'
            ),
        )
    )

    body = result.model_dump(mode="json", exclude_none=True)
    assert body["id"] == "resp_2"
    assert body["output"] == [
        {
            "id": "mcp_1",
            "type": "mcp_approval_request",
            "status": "in_progress",
            "request_id": "req_1",
            "server_label": "github",
            "tool_name": "repos/list",
        }
    ]


@pytest.mark.asyncio
async def test_collect_responses_payload_preserves_output_image_item() -> None:
    result = await proxy_api_module._collect_responses_payload(
        _iter_blocks(
            (
                'data: {"type":"response.output_item.done","output_index":0,'
                '"item":{"id":"img_1","type":"output_image","image_url":"https://example.com/a.png"}}\n\n'
            ),
            (
                'data: {"type":"response.completed","response":{"id":"resp_3","object":"response",'
                '"status":"completed","output":[]}}\n\n'
            ),
        )
    )

    body = result.model_dump(mode="json", exclude_none=True)
    assert body["id"] == "resp_3"
    assert body["output"] == [
        {
            "id": "img_1",
            "type": "output_image",
            "image_url": "https://example.com/a.png",
        }
    ]
