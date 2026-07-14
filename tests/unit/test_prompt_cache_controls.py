from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.exceptions import ProxyInvalidRequest, ProxyModelNotAllowed
from app.core.openai.exceptions import ClientPayloadError
from app.core.openai.models import OpenAIEvent
from app.core.openai.requests import ResponsesCompactRequest, ResponsesRequest
from app.core.openai.v1_requests import V1ResponsesCompactRequest, V1ResponsesRequest
from app.core.utils.time import utcnow
from app.modules.api_keys.service import ApiKeyData
from app.modules.proxy.request_policy import apply_api_key_enforcement


def _message_with_breakpoint(block_type: str) -> dict[str, object]:
    block: dict[str, object] = {
        "type": block_type,
        "prompt_cache_breakpoint": {"mode": "explicit"},
    }
    if block_type == "input_text":
        block["text"] = "stable prefix"
    elif block_type == "input_image":
        block["image_url"] = "https://example.com/image.png"
    else:
        block["file_url"] = "https://example.com/reference.pdf"
    return {"role": "user", "content": [block]}


@pytest.mark.parametrize("block_type", ["input_text", "input_image", "input_file"])
def test_responses_request_preserves_gpt56_prompt_cache_controls(block_type: str) -> None:
    input_item = _message_with_breakpoint(block_type)

    payload = ResponsesRequest.model_validate(
        {
            "model": "gpt-5.6-sol",
            "input": [input_item, {"role": "user", "content": [{"type": "input_text", "text": "dynamic"}]}],
            "prompt_cache_key": "semia:rules-v3:shard-03",
            "prompt_cache_options": {"mode": "explicit", "ttl": "30m"},
        }
    ).to_payload()

    assert payload["prompt_cache_key"] == "semia:rules-v3:shard-03"
    assert payload["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    assert payload["input"] == [
        input_item,
        {"role": "user", "content": [{"type": "input_text", "text": "dynamic"}]},
    ]
    assert "prompt_cache_retention" not in payload


def test_explicit_mode_without_breakpoint_does_not_insert_one() -> None:
    payload = ResponsesRequest.model_validate(
        {
            "model": "gpt-5.6-sol",
            "input": "dynamic only",
            "prompt_cache_options": {"mode": "explicit"},
        }
    ).to_payload()

    assert payload["prompt_cache_options"] == {"mode": "explicit"}
    assert "prompt_cache_breakpoint" not in str(payload["input"])


@pytest.mark.parametrize("field", ["mode", "ttl"])
def test_prompt_cache_options_reject_explicit_null_members(field: str) -> None:
    with pytest.raises(ValidationError, match="cannot be null"):
        ResponsesRequest.model_validate(
            {
                "model": "gpt-5.6-sol",
                "input": "hello",
                "prompt_cache_options": {field: None},
            }
        )


@pytest.mark.parametrize(
    ("request_type", "payload_data"),
    [
        (ResponsesRequest, {"model": "gpt-5.6-sol", "input": "hello"}),
        (ResponsesCompactRequest, {"model": "gpt-5.6-sol", "input": "hello"}),
        (V1ResponsesRequest, {"model": "gpt-5.6-sol", "input": "hello"}),
        (V1ResponsesCompactRequest, {"model": "gpt-5.6-sol", "input": "hello"}),
    ],
)
def test_request_models_reject_explicit_null_prompt_cache_options(
    request_type: type[ResponsesRequest]
    | type[ResponsesCompactRequest]
    | type[V1ResponsesRequest]
    | type[V1ResponsesCompactRequest],
    payload_data: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="prompt_cache_options cannot be null"):
        request_type.model_validate({**payload_data, "prompt_cache_options": None})


@pytest.mark.parametrize(
    "options",
    [
        {"mode": "automatic"},
        {"ttl": "24h"},
        {"mode": "explicit", "future": True},
    ],
)
def test_responses_request_rejects_invalid_prompt_cache_options(options: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ResponsesRequest.model_validate(
            {
                "model": "gpt-5.6-sol",
                "input": "hello",
                "prompt_cache_options": options,
            }
        )


@pytest.mark.parametrize(
    "content_block",
    [
        {
            "type": "input_text",
            "text": "stable",
            "prompt_cache_breakpoint": {"mode": "implicit"},
        },
        {
            "type": "input_text",
            "text": "stable",
            "prompt_cache_breakpoint": {"mode": "explicit", "future": True},
        },
        {
            "type": "output_text",
            "text": "stable",
            "prompt_cache_breakpoint": {"mode": "explicit"},
        },
        {
            "type": "input_text",
            "text": "stable",
            "prompt_cache_breakpoint": None,
        },
        {
            "type": "reasoning",
            "text": "discarded during input sanitization",
            "prompt_cache_breakpoint": {"mode": "explicit"},
        },
    ],
)
def test_responses_request_rejects_invalid_prompt_cache_breakpoint(content_block: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ResponsesRequest.model_validate(
            {
                "model": "gpt-5.6-sol",
                "input": [{"role": "user", "content": [content_block]}],
            }
        )


@pytest.mark.parametrize("model", ["gpt-5.5", "gpt-5.4", "o3"])
@pytest.mark.parametrize("control", ["options", "breakpoint"])
def test_older_models_reject_gpt56_prompt_cache_controls(model: str, control: str) -> None:
    request: dict[str, object] = {"model": model, "input": "hello"}
    if control == "options":
        request["prompt_cache_options"] = {"mode": "explicit"}
    else:
        request["input"] = [_message_with_breakpoint("input_text")]

    payload = ResponsesRequest.model_validate(request)

    with pytest.raises(ProxyInvalidRequest, match="require GPT-5.6 or later"):
        apply_api_key_enforcement(payload, None)


@pytest.mark.parametrize("model", ["gpt-5.6", "gpt-5.7-sol", "gpt-6", "gpt-6-codex"])
def test_gpt56_and_later_models_accept_prompt_cache_controls(model: str) -> None:
    payload = ResponsesRequest.model_validate(
        {
            "model": model,
            "input": "hello",
            "prompt_cache_options": {"mode": "explicit"},
        }
    )

    assert payload.prompt_cache_options is not None
    assert payload.prompt_cache_options.mode == "explicit"


def test_api_key_enforced_older_model_rejects_gpt56_prompt_cache_controls() -> None:
    request = ResponsesRequest.model_validate(
        {
            "model": "gpt-5.6-sol",
            "input": [_message_with_breakpoint("input_text")],
            "prompt_cache_options": {"mode": "explicit", "ttl": "30m"},
        }
    )
    api_key = ApiKeyData(
        id="key_prompt_cache_old_model",
        name="prompt-cache-old-model",
        key_prefix="sk-clb-test",
        allowed_models=None,
        enforced_model="gpt-5.5",
        enforced_reasoning_effort=None,
        enforced_service_tier=None,
        expires_at=None,
        is_active=True,
        created_at=utcnow(),
        last_used_at=None,
    )

    with pytest.raises(ProxyModelNotAllowed, match="does not support GPT-5.6 prompt cache controls"):
        apply_api_key_enforcement(request, api_key)

    assert request.model == "gpt-5.5"


@pytest.mark.parametrize("request_type", [ResponsesRequest, ResponsesCompactRequest])
def test_api_key_enforced_gpt56_model_accepts_controls_from_older_requested_model(request_type) -> None:
    request = request_type.model_validate(
        {
            "model": "gpt-5.5",
            "input": [_message_with_breakpoint("input_text")],
            "prompt_cache_options": {"mode": "explicit", "ttl": "30m"},
        }
    )
    api_key = ApiKeyData(
        id="key_prompt_cache_gpt56_model",
        name="prompt-cache-gpt56-model",
        key_prefix="sk-clb-test",
        allowed_models=["gpt-5.6-sol"],
        enforced_model="gpt-5.6-sol",
        enforced_reasoning_effort=None,
        enforced_service_tier=None,
        expires_at=None,
        is_active=True,
        created_at=utcnow(),
        last_used_at=None,
    )

    apply_api_key_enforcement(request, api_key)

    assert request.model == "gpt-5.6-sol"
    assert request.to_payload()["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}


def test_assistant_input_text_breakpoint_is_rejected_before_normalization() -> None:
    with pytest.raises(ValidationError, match="assistant or tool input messages"):
        ResponsesRequest.model_validate(
            {
                "model": "gpt-5.6-sol",
                "input": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "would otherwise become output_text",
                                "prompt_cache_breakpoint": {"mode": "explicit"},
                            }
                        ],
                    }
                ],
            }
        )


def test_v1_messages_preserve_user_prompt_cache_breakpoint() -> None:
    content_block = {
        "type": "input_text",
        "text": "stable member prefix",
        "prompt_cache_breakpoint": {"mode": "explicit"},
    }

    payload = V1ResponsesRequest.model_validate(
        {
            "model": "gpt-5.6-sol",
            "messages": [{"role": "user", "content": [content_block]}],
            "prompt_cache_options": {"mode": "explicit", "ttl": "30m"},
        }
    ).to_responses_request()

    assert payload.to_payload()["input"] == [{"role": "user", "content": [content_block]}]


def test_v1_messages_reject_system_breakpoint_that_would_move_into_instructions() -> None:
    with pytest.raises(ClientPayloadError, match="requires structured Responses input"):
        V1ResponsesRequest.model_validate(
            {
                "model": "gpt-5.6-sol",
                "messages": [
                    {
                        "role": "system",
                        "content": [
                            {
                                "type": "text",
                                "text": "stable system prefix",
                                "prompt_cache_breakpoint": {"mode": "explicit"},
                            }
                        ],
                    }
                ],
                "prompt_cache_options": {"mode": "explicit"},
            }
        ).to_responses_request()


def test_v1_and_compact_requests_preserve_prompt_cache_options() -> None:
    input_items = [_message_with_breakpoint("input_text")]

    v1_payload = V1ResponsesRequest.model_validate(
        {
            "model": "gpt-5.6-sol",
            "input": input_items,
            "prompt_cache_options": {"mode": "implicit", "ttl": "30m"},
        }
    ).to_responses_request()
    compact_payload = V1ResponsesCompactRequest.model_validate(
        {
            "model": "gpt-5.6-sol",
            "input": input_items,
            "prompt_cache_key": "compact-cache-key",
            "prompt_cache_options": {"mode": "explicit", "ttl": "30m"},
        }
    ).to_compact_request()

    assert v1_payload.to_payload()["prompt_cache_options"] == {"mode": "implicit", "ttl": "30m"}
    assert compact_payload.to_payload()["prompt_cache_key"] == "compact-cache-key"
    assert compact_payload.to_payload()["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    assert isinstance(compact_payload, ResponsesCompactRequest)


def test_response_usage_types_and_preserves_cache_write_tokens() -> None:
    event = OpenAIEvent.model_validate(
        {
            "type": "response.completed",
            "response": {
                "id": "resp_cache_usage",
                "usage": {
                    "input_tokens": 3072,
                    "input_tokens_details": {
                        "cached_tokens": 1024,
                        "cache_write_tokens": 2048,
                    },
                },
            },
        }
    )

    assert event.response is not None
    assert event.response.usage is not None
    assert event.response.usage.input_tokens_details is not None
    assert event.response.usage.input_tokens_details.cached_tokens == 1024
    assert event.response.usage.input_tokens_details.cache_write_tokens == 2048
    assert event.model_dump(mode="json", exclude_none=True)["response"]["usage"]["input_tokens_details"] == {
        "cached_tokens": 1024,
        "cache_write_tokens": 2048,
    }
