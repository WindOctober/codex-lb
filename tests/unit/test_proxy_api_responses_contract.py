from __future__ import annotations

import asyncio
import gc
from collections.abc import AsyncIterator

import anyio
import pytest

import app.modules.proxy.api as proxy_api_module

pytestmark = pytest.mark.unit


async def _iter_blocks(*blocks: str) -> AsyncIterator[str]:
    for block in blocks:
        yield block


class _ClosableBlocks:
    def __init__(self, *blocks: str) -> None:
        self._blocks = iter(blocks)
        self.close_calls = 0

    def __aiter__(self) -> _ClosableBlocks:
        return self

    async def __anext__(self) -> str:
        try:
            return next(self._blocks)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def aclose(self) -> None:
        self.close_calls += 1


class _CancellationAwareClosableBlocks(_ClosableBlocks):
    def __init__(self, *blocks: str) -> None:
        super().__init__(*blocks)
        self.close_started = False
        self.close_finished = False

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_started = True
        await anyio.sleep(0)
        self.close_finished = True


class _DirectTaskCancellationClosableBlocks(_ClosableBlocks):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.close_finished = False

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        await self.allow_close.wait()
        self.close_finished = True


@pytest.mark.asyncio
async def test_public_responses_wrapper_chain_closes_child_once_after_first_event() -> None:
    child = _ClosableBlocks(
        'data: {"type":"response.created","response":{"id":"resp_wrapper"}}\n\n',
    )
    normalized = proxy_api_module._normalize_public_responses_stream(child)
    first = await normalized.__anext__()
    public_stream = proxy_api_module._prepend_first(first, normalized)

    assert await public_stream.__anext__() == first
    await public_stream.aclose()

    assert child.close_calls == 1


@pytest.mark.asyncio
async def test_public_responses_wrapper_chain_shields_child_close_from_cancellation_scope() -> None:
    child = _CancellationAwareClosableBlocks(
        'data: {"type":"response.created","response":{"id":"resp_cancelled_wrapper"}}\n\n',
    )
    normalized = proxy_api_module._normalize_public_responses_stream(child)
    first = await normalized.__anext__()
    public_stream = proxy_api_module._prepend_first(first, normalized)

    assert await public_stream.__anext__() == first
    with anyio.CancelScope() as cancel_scope:
        cancel_scope.cancel()
        await public_stream.aclose()

    assert child.close_started is True
    assert child.close_finished is True
    assert child.close_calls == 1


@pytest.mark.asyncio
async def test_public_responses_child_close_finishes_after_direct_task_cancellation() -> None:
    child = _DirectTaskCancellationClosableBlocks()
    close_task = asyncio.create_task(proxy_api_module._close_async_iterator(child))
    await asyncio.wait_for(child.close_started.wait(), timeout=1.0)

    close_task.cancel()
    child.allow_close.set()

    with pytest.raises(asyncio.CancelledError):
        await close_task
    assert child.close_finished is True
    assert child.close_calls == 1


@pytest.mark.asyncio
async def test_owned_streaming_response_closes_wrapper_chain_on_asgi_disconnect() -> None:
    child_closed = 0
    send_started = anyio.Event()
    loop_errors: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(dict(context)))

    async def child_stream() -> AsyncIterator[str]:
        nonlocal child_closed
        try:
            yield 'data: {"type":"response.created","response":{"id":"resp_disconnect"}}\n\n'
            await anyio.sleep_forever()
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.sleep(0)
                child_closed += 1

    normalized = proxy_api_module._normalize_public_responses_stream(child_stream())
    first = await anext(normalized)
    public_stream = proxy_api_module._prepend_first(first, normalized)
    response = proxy_api_module._OwnedStreamingResponse(public_stream)

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body" and message.get("more_body") is True:
            send_started.set()
            await anyio.sleep_forever()

    async def receive() -> dict[str, str]:
        await send_started.wait()
        return {"type": "http.disconnect"}

    try:
        await response(
            {"type": "http", "asgi": {"spec_version": "2.3"}},
            receive,
            send,
        )
        del response, public_stream, normalized
        for _ in range(4):
            gc.collect()
            await anyio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert child_closed == 1
    assert loop_errors == []


@pytest.mark.asyncio
async def test_normalize_public_responses_stream_closes_child_after_normal_completion() -> None:
    child = _ClosableBlocks(
        'data: {"type":"response.completed","response":{"id":"resp_done","output":[]}}\n\n',
    )

    blocks = [block async for block in proxy_api_module._normalize_public_responses_stream(child)]

    assert len(blocks) == 1
    assert child.close_calls == 1


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
