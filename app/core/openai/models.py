from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
)

from app.core.types import JsonValue

type ModelLikeInput = JsonValue | BaseModel


def _normalize_model_value[T: BaseModel](model_type: type[T], value: ModelLikeInput | None) -> T | None:
    if value is None:
        return None
    try:
        return model_type.model_validate(value)
    except ValidationError:
        return None


class OpenAIError(BaseModel):
    model_config = ConfigDict(extra="allow")

    message: StrictStr | None = None
    type: StrictStr | None = None
    code: StrictStr | None = None
    param: StrictStr | None = None
    plan_type: StrictStr | None = None
    resets_at: StrictInt | StrictFloat | None = None
    resets_in_seconds: StrictInt | StrictFloat | None = None


class OpenAIErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    error: OpenAIError | None = None


class ResponseUsageDetails(BaseModel):
    model_config = ConfigDict(extra="allow")

    cached_tokens: StrictInt | None = None
    cache_write_tokens: StrictInt | None = None
    reasoning_tokens: StrictInt | None = None


class ResponseUsage(BaseModel):
    model_config = ConfigDict(extra="allow")

    input_tokens: StrictInt | None = None
    output_tokens: StrictInt | None = None
    total_tokens: StrictInt | None = None
    input_tokens_details: ResponseUsageDetails | None = None
    output_tokens_details: ResponseUsageDetails | None = None


@dataclass(frozen=True, slots=True)
class NormalizedResponseUsage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int


def normalize_response_usage(usage: ResponseUsage | None) -> NormalizedResponseUsage | None:
    """Return a conservative, internally consistent lower bound for partial usage."""
    if usage is None:
        return None
    details = usage.input_tokens_details
    output_details = usage.output_tokens_details
    cached_tokens = max(0, details.cached_tokens or 0) if details is not None else 0
    cache_write_tokens = max(0, details.cache_write_tokens or 0) if details is not None else 0
    reasoning_tokens = max(0, output_details.reasoning_tokens or 0) if output_details is not None else 0
    raw_input_tokens = usage.input_tokens
    raw_output_tokens = usage.output_tokens
    raw_total_tokens = usage.total_tokens
    has_authoritative_usage = any(
        value is not None
        for value in (
            raw_input_tokens,
            raw_output_tokens,
            raw_total_tokens,
            details.cached_tokens if details is not None else None,
            details.cache_write_tokens if details is not None else None,
            output_details.reasoning_tokens if output_details is not None else None,
        )
    )
    if not has_authoritative_usage:
        return None

    input_lower_bound = cached_tokens + cache_write_tokens
    output_tokens = max(0, raw_output_tokens) if raw_output_tokens is not None else reasoning_tokens
    input_tokens = max(0, raw_input_tokens) if raw_input_tokens is not None else input_lower_bound
    total_tokens = max(0, raw_total_tokens) if raw_total_tokens is not None else None
    if total_tokens is not None:
        if raw_input_tokens is None:
            input_tokens = max(input_tokens, total_tokens - output_tokens)
        if raw_output_tokens is None:
            output_tokens = max(output_tokens, total_tokens - input_tokens)
    input_tokens = max(input_tokens, input_lower_bound)
    cached_tokens = min(cached_tokens, input_tokens)
    cache_write_tokens = min(cache_write_tokens, input_tokens - cached_tokens)
    return NormalizedResponseUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_tokens,
        cache_write_tokens=cache_write_tokens,
    )


class OpenAIResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: StrictStr | None = None
    status: StrictStr | None = None
    error: OpenAIError | None = None
    usage: ResponseUsage | None = None

    @field_validator("error", mode="before")
    @classmethod
    def _normalize_error(cls, value: ModelLikeInput | None) -> OpenAIError | None:
        return _normalize_model_value(OpenAIError, value)

    @field_validator("usage", mode="before")
    @classmethod
    def _normalize_usage(cls, value: ModelLikeInput | None) -> ResponseUsage | None:
        return _normalize_model_value(ResponseUsage, value)


class OpenAIEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: StrictStr
    response: OpenAIResponse | None = None
    error: OpenAIError | None = None

    @field_validator("error", mode="before")
    @classmethod
    def _normalize_error(cls, value: ModelLikeInput | None) -> OpenAIError | None:
        return _normalize_model_value(OpenAIError, value)


class OpenAIResponsePayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: StrictStr | None = None
    status: StrictStr | None = None
    error: OpenAIError | None = None
    usage: ResponseUsage | None = None

    @field_validator("error", mode="before")
    @classmethod
    def _normalize_error(cls, value: ModelLikeInput | None) -> OpenAIError | None:
        return _normalize_model_value(OpenAIError, value)

    @field_validator("usage", mode="before")
    @classmethod
    def _normalize_usage(cls, value: ModelLikeInput | None) -> ResponseUsage | None:
        return _normalize_model_value(ResponseUsage, value)


class CompactResponsePayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    object: StrictStr
    id: StrictStr | None = None
    status: StrictStr | None = None
    error: OpenAIError | None = None
    usage: ResponseUsage | None = None

    @field_validator("object")
    @classmethod
    def _validate_object(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Compact response payload requires an object discriminator")
        if not normalized.startswith("response.compact"):
            raise ValueError("Compact response payload requires a compact object discriminator")
        return normalized

    @field_validator("error", mode="before")
    @classmethod
    def _normalize_error(cls, value: ModelLikeInput | None) -> OpenAIError | None:
        return _normalize_model_value(OpenAIError, value)

    @field_validator("usage", mode="before")
    @classmethod
    def _normalize_usage(cls, value: ModelLikeInput | None) -> ResponseUsage | None:
        return _normalize_model_value(ResponseUsage, value)


OpenAIResponseResult: TypeAlias = OpenAIResponsePayload | OpenAIErrorEnvelope
CompactResponseResult: TypeAlias = CompactResponsePayload | OpenAIErrorEnvelope
