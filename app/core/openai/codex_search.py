from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.types import JsonObject, JsonValue


class CodexSearchRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    reasoning: JsonValue | None = None
    input: JsonValue | None = None
    commands: JsonObject | None = None
    settings: JsonObject | None = None
    max_output_tokens: int | None = Field(default=None, gt=0)

    @field_validator("id", "model")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    def to_payload(self) -> dict[str, JsonValue]:
        return self.model_dump(mode="json", exclude_none=True)


class CodexSearchResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    encrypted_output: str | None = None
    output: str = Field(min_length=1)

    def to_payload(self) -> dict[str, JsonValue]:
        return self.model_dump(mode="json", exclude_none=True)
