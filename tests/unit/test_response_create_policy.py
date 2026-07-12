from __future__ import annotations

from copy import deepcopy

from app.core.clients import proxy as proxy_client
from app.core.openai import response_create as policy
from app.core.openai.requests import ResponsesRequest
from app.modules.proxy import service as proxy_service


def test_legacy_modules_share_canonical_slimming_function() -> None:
    assert proxy_client._slim_response_create_payload_for_upstream is policy._slim_response_create_payload_for_upstream
    assert proxy_service._slim_response_create_payload_for_upstream is policy._slim_response_create_payload_for_upstream


def test_slimming_preserves_latest_user_suffix_and_does_not_mutate_input() -> None:
    large_output = "x" * (32 * 1024 + 1)
    payload = {
        "input": [
            {"type": "function_call_output", "call_id": "old", "output": large_output},
            {
                "role": "assistant",
                "content": [{"type": "input_image", "image_url": "data:image/png;base64,old"}],
            },
            {"role": "user", "content": [{"type": "input_text", "text": "latest"}]},
            {"role": "assistant", "content": [{"type": "output_text", "text": "recent"}]},
        ]
    }
    original = deepcopy(payload)

    slimmed, summary = policy._slim_response_create_payload_for_upstream(payload, max_bytes=1)

    assert payload == original
    assert summary == {
        "historical_tool_outputs_slimmed": 1,
        "historical_images_slimmed": 1,
    }
    slimmed_input = slimmed["input"]
    assert isinstance(slimmed_input, list)
    assert slimmed_input[0]["output"] == policy._RESPONSE_CREATE_TOOL_OUTPUT_OMISSION_NOTICE.format(
        bytes=len(large_output.encode("utf-8"))
    )
    assert slimmed_input[1]["content"] == [
        {"type": "input_text", "text": policy._RESPONSE_CREATE_IMAGE_OMISSION_NOTICE}
    ]
    assert slimmed_input[2:] == original["input"][2:]


def test_slimming_without_user_anchor_preserves_payload_identity() -> None:
    payload = {
        "input": [
            {"type": "function_call_output", "call_id": "old", "output": "x" * (32 * 1024 + 1)},
        ]
    }

    slimmed, summary = policy._slim_response_create_payload_for_upstream(payload, max_bytes=1)

    assert slimmed is payload
    assert summary is None


def test_payload_too_large_error_contract_is_shared() -> None:
    payload = policy._response_create_too_large_error_envelope(17, 16)

    assert payload == proxy_client._response_create_too_large_error_envelope(17, 16)
    assert payload == proxy_service._response_create_too_large_error_envelope(17, 16)
    assert payload["error"] == {
        "code": "payload_too_large",
        "message": (
            "response.create is too large for upstream websocket (17 bytes > 16 bytes). "
            "Reduce historical images/screenshots or compact the thread."
        ),
        "type": "invalid_request_error",
        "param": "input",
    }


def test_image_capability_detection_uses_nested_input_and_tools() -> None:
    payload = ResponsesRequest.model_validate(
        {
            "model": "gpt-5.6-sol",
            "instructions": "",
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": "https://example.test/image.png"}],
                }
            ],
            "tools": [{"type": "image_generation"}],
            "stream": True,
        }
    )

    assert policy._responses_request_contains_input_image(payload) is True
    assert policy._responses_request_uses_image_generation(payload) is True
