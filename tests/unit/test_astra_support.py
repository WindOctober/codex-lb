from __future__ import annotations

import pytest

from app.core.openai.model_registry import ModelRegistry
from app.core.openai.requests import ResponsesRequest
from app.core.usage.pricing import UsageTokens, calculate_cost_from_usage, get_pricing_for_model

pytestmark = pytest.mark.unit


def test_astra_codex_metadata_and_transport() -> None:
    registry = ModelRegistry()
    astra = registry.get_models_with_fallback()["gpt-6-astra"]
    assert astra.display_name == "GPT-6-Astra"
    assert astra.context_window == 272_000
    assert astra.raw["max_context_window"] == 872_000
    assert astra.default_reasoning_level == "medium"
    assert {level.effort for level in astra.supported_reasoning_levels} == {
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
    }
    assert astra.input_modalities == ("text", "image")
    assert astra.raw["visibility"] == "list"
    assert astra.raw["shell_type"] == "unified_exec"
    assert registry.prefers_websockets("gpt-6-astra")
    assert "free" not in astra.available_in_plans


async def test_upstream_snapshot_supersedes_astra_bootstrap() -> None:
    registry = ModelRegistry()
    sol = registry.get_models_with_fallback()["gpt-5.6-sol"]
    await registry.update({"pro": [sol]})
    assert "gpt-6-astra" not in registry.get_models_with_fallback()
    assert registry.plan_types_for_model("gpt-6-astra") == frozenset()


def test_astra_responses_request_preserves_model_and_cache_controls() -> None:
    request = ResponsesRequest.model_validate(
        {
            "model": "gpt-6-astra",
            "input": "Reply OK",
            "reasoning": {"effort": "medium"},
            "prompt_cache_options": {"mode": "explicit"},
        }
    )
    payload = request.to_payload()
    assert payload["model"] == "gpt-6-astra"
    assert payload["prompt_cache_options"] == {"mode": "explicit"}


@pytest.mark.parametrize("tier, expected", [(None, 1.05), ("priority", 2.1), ("fast", 2.1), ("flex", 0.525)])
def test_astra_pricing_by_tier(tier: str | None, expected: float) -> None:
    resolved = get_pricing_for_model("gpt-6-astra")
    assert resolved is not None
    _, price = resolved
    usage = UsageTokens(input_tokens=100_000, output_tokens=1_000, cached_input_tokens=0)
    assert calculate_cost_from_usage(usage, price, service_tier=tier) == pytest.approx(expected)


def test_astra_long_context_cache_and_alias_pricing() -> None:
    resolved = get_pricing_for_model("gpt-6-astra-2026-09-05")
    assert resolved is not None
    model, price = resolved
    assert model == "gpt-6-astra"
    assert get_pricing_for_model("gpt-6-unknown") is None
    assert calculate_cost_from_usage(
        UsageTokens(input_tokens=300_000, output_tokens=1_000, cached_input_tokens=0),
        price,
    ) == pytest.approx(6.075)
    assert calculate_cost_from_usage(
        UsageTokens(input_tokens=100_000, output_tokens=1_000, cached_input_tokens=50_000),
        price,
    ) == pytest.approx(0.6)
    assert calculate_cost_from_usage(
        UsageTokens(input_tokens=100_000, output_tokens=1_000, cached_input_tokens=0, cache_write_tokens=40_000),
        price,
    ) == pytest.approx(1.15)
