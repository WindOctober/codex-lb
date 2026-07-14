from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import cast

from app.core.errors import OpenAIErrorEnvelope, openai_error
from app.core.openai.requests import ResponsesRequest
from app.core.types import JsonObject, JsonValue
from app.core.utils.json_guards import is_json_mapping

_RESPONSE_CREATE_TOOL_OUTPUT_OMISSION_NOTICE = (
    "[codex-lb omitted historical tool output ({bytes} bytes) to fit upstream websocket budget]"
)
_RESPONSE_CREATE_IMAGE_OMISSION_NOTICE = "[codex-lb omitted historical inline image to fit upstream websocket budget]"


def _response_create_too_large_error_envelope(actual_bytes: int, max_bytes: int) -> OpenAIErrorEnvelope:
    payload = openai_error(
        "payload_too_large",
        (
            "response.create is too large for upstream websocket "
            f"({actual_bytes} bytes > {max_bytes} bytes). "
            "Reduce historical images/screenshots or compact the thread."
        ),
        error_type="invalid_request_error",
    )
    payload["error"]["param"] = "input"
    return payload


def _slim_response_create_payload_for_upstream(
    payload: JsonObject,
    *,
    max_bytes: int,
) -> tuple[JsonObject, dict[str, int] | None]:
    del max_bytes
    input_value = payload.get("input")
    if not isinstance(input_value, list) or not input_value:
        return payload, None

    input_items = cast(list[JsonValue], deepcopy(input_value))
    preserve_from = _response_create_recent_suffix_start(input_items)
    historical = input_items[:preserve_from]
    recent = input_items[preserve_from:]

    tool_outputs_slimmed = 0
    images_slimmed = 0
    slimmed_historical: list[JsonValue] = []
    for item in historical:
        slimmed_item, item_tool_outputs_slimmed, item_images_slimmed = _slim_historical_response_input_item(item)
        tool_outputs_slimmed += item_tool_outputs_slimmed
        images_slimmed += item_images_slimmed
        slimmed_historical.append(slimmed_item)

    if tool_outputs_slimmed == 0 and images_slimmed == 0:
        return payload, None

    candidate_payload = dict(payload)
    candidate_payload["input"] = slimmed_historical + recent
    return candidate_payload, {
        "historical_tool_outputs_slimmed": tool_outputs_slimmed,
        "historical_images_slimmed": images_slimmed,
    }


def _response_create_recent_suffix_start(input_items: list[JsonValue]) -> int:
    last_user_index: int | None = None
    for index, item in enumerate(input_items):
        if is_json_mapping(item) and item.get("role") == "user":
            last_user_index = index
    return last_user_index if last_user_index is not None else 0


def _slim_historical_response_input_item(item: JsonValue) -> tuple[JsonValue, int, int]:
    if not is_json_mapping(item):
        return item, 0, 0

    item_mapping = dict(cast(dict[str, JsonValue], deepcopy(item)))
    tool_outputs_slimmed = 0
    images_slimmed = 0

    if item_mapping.get("type") == "function_call_output":
        output = item_mapping.get("output")
        output_text = output if isinstance(output, str) else None
        if output_text is not None and _should_slim_historical_tool_output(output_text):
            item_mapping["output"] = _RESPONSE_CREATE_TOOL_OUTPUT_OMISSION_NOTICE.format(
                bytes=len(output_text.encode("utf-8"))
            )
            tool_outputs_slimmed += 1

    content = item_mapping.get("content")
    slimmed_content, content_images_slimmed = _slim_historical_response_content(content)
    if content_images_slimmed > 0:
        item_mapping["content"] = slimmed_content
        images_slimmed += content_images_slimmed

    if item_mapping.get("type") == "input_image" and _is_inline_image_reference(item_mapping.get("image_url")):
        return _response_create_inline_image_notice_item(item_mapping), tool_outputs_slimmed, images_slimmed + 1

    return item_mapping, tool_outputs_slimmed, images_slimmed


def _slim_historical_response_content(content: JsonValue) -> tuple[JsonValue, int]:
    if is_json_mapping(content):
        return _slim_historical_response_content_part(content)
    if not isinstance(content, list):
        return content, 0

    slimmed_parts: list[JsonValue] = []
    images_slimmed = 0
    for part in content:
        slimmed_part, part_images_slimmed = _slim_historical_response_content_part(part)
        slimmed_parts.append(slimmed_part)
        images_slimmed += part_images_slimmed
    return slimmed_parts, images_slimmed


def _slim_historical_response_content_part(part: JsonValue) -> tuple[JsonValue, int]:
    if not is_json_mapping(part):
        return part, 0

    part_mapping = dict(cast(dict[str, JsonValue], deepcopy(part)))
    part_type = part_mapping.get("type")
    if part_type == "input_image" and _is_inline_image_reference(part_mapping.get("image_url")):
        return _response_create_inline_image_notice_part(part_mapping), 1

    if part_type == "image_url":
        image_url_value = part_mapping.get("image_url")
        image_url = image_url_value.get("url") if is_json_mapping(image_url_value) else image_url_value
        if _is_inline_image_reference(image_url):
            return _response_create_inline_image_notice_part(), 1

    return part_mapping, 0


def _response_create_inline_image_notice_part(source: dict[str, JsonValue] | None = None) -> JsonObject:
    notice: dict[str, JsonValue] = {"type": "input_text", "text": _RESPONSE_CREATE_IMAGE_OMISSION_NOTICE}
    if source is not None and "prompt_cache_breakpoint" in source:
        notice["prompt_cache_breakpoint"] = deepcopy(source["prompt_cache_breakpoint"])
    return cast(JsonObject, notice)


def _response_create_inline_image_notice_item(source: dict[str, JsonValue]) -> JsonObject:
    return {"role": "user", "content": [_response_create_inline_image_notice_part(source)]}


def _responses_request_contains_input_image(payload: ResponsesRequest) -> bool:
    input_value = payload.input
    return isinstance(input_value, list) and any(_json_value_contains_input_image_part(item) for item in input_value)


def _json_value_contains_input_image_part(value: JsonValue) -> bool:
    if is_json_mapping(value):
        if value.get("type") == "input_image":
            return True
        return any(_json_value_contains_input_image_part(child) for child in value.values())
    if isinstance(value, list):
        return any(_json_value_contains_input_image_part(item) for item in value)
    return False


def _responses_request_uses_image_generation(payload: ResponsesRequest) -> bool:
    tools = payload.tools
    return isinstance(tools, list) and any(
        is_json_mapping(tool) and tool.get("type") == "image_generation" for tool in tools
    )


def _is_inline_image_reference(value: JsonValue) -> bool:
    return isinstance(value, str) and value.startswith("data:image/")


def _should_slim_historical_tool_output(output: str) -> bool:
    return "data:image/" in output or len(output.encode("utf-8")) > 32 * 1024


def _should_dump_oversized_response_create(error_code: str, error_message: str | None) -> bool:
    if error_code != "stream_incomplete" or not error_message:
        return False
    normalized = error_message.lower()
    return "1009" in normalized or "message too big" in normalized


def _safe_dump_slug(value: str | None, *, fallback: str) -> str:
    if not value:
        return fallback
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return normalized[:80] if normalized else fallback


def _summarize_response_create_payload(
    payload: JsonObject,
    *,
    largest_items_limit: int = 10,
) -> JsonObject:
    field_sizes = sorted(
        ({"key": key, "size_bytes": _json_size_bytes(value)} for key, value in payload.items()),
        key=lambda item: int(item["size_bytes"]),
        reverse=True,
    )
    summary: dict[str, JsonValue] = {
        "top_level_keys": list(payload.keys()),
        "top_level_field_sizes": field_sizes,
    }
    input_summary = _summarize_response_create_input(
        payload.get("input"),
        largest_items_limit=largest_items_limit,
    )
    if input_summary is not None:
        summary["input"] = input_summary
    return summary


def _summarize_response_create_input(
    input_value: JsonValue,
    *,
    largest_items_limit: int = 10,
) -> JsonObject | None:
    if not isinstance(input_value, list):
        return None

    role_counts: dict[str, int] = {}
    item_type_counts: dict[str, int] = {}
    content_part_type_counts: dict[str, int] = {}
    largest_items: list[dict[str, JsonValue]] = []
    for index, item in enumerate(input_value):
        item_summary: dict[str, JsonValue] = {"index": index, "size_bytes": _json_size_bytes(item)}
        if isinstance(item, dict):
            role = item.get("role")
            if isinstance(role, str):
                item_summary["role"] = role
                role_counts[role] = role_counts.get(role, 0) + 1
            item_type = item.get("type")
            if isinstance(item_type, str):
                item_summary["type"] = item_type
                item_type_counts[item_type] = item_type_counts.get(item_type, 0) + 1
            content = item.get("content")
            if isinstance(content, list):
                item_summary["content_parts"] = len(content)
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    part_type = part.get("type")
                    if isinstance(part_type, str):
                        content_part_type_counts[part_type] = content_part_type_counts.get(part_type, 0) + 1
        largest_items.append(item_summary)

    largest_items.sort(key=lambda item: int(item["size_bytes"]), reverse=True)
    return {
        "count": len(input_value),
        "role_counts": cast(JsonValue, role_counts),
        "item_type_counts": cast(JsonValue, item_type_counts),
        "content_part_type_counts": cast(JsonValue, content_part_type_counts),
        "largest_items": cast(JsonValue, largest_items[:largest_items_limit]),
    }


def _json_size_bytes(value: JsonValue) -> int:
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
