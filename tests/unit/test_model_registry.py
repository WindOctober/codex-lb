from __future__ import annotations

import time
from dataclasses import replace

import pytest

from app.core.openai.model_registry import ModelRegistry, ReasoningLevel, UpstreamModel, is_public_model

pytestmark = pytest.mark.unit

BOOTSTRAP_MODEL_SLUGS = {
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.2",
    "codex-auto-review",
}

EXPECTED_CORE_MODEL_PLANS = {
    "plus",
    "pro",
    "prolite",
    "team",
    "business",
    "enterprise",
    "edu",
    "education",
    "go",
    "hc",
    "finserv",
    "quorum",
    "self_serve_business_usage_based",
    "enterprise_cbp_usage_based",
}


def _model(slug: str) -> UpstreamModel:
    return UpstreamModel(
        slug=slug,
        display_name=slug,
        description=f"Model {slug}",
        context_window=128000,
        input_modalities=("text",),
        supported_reasoning_levels=(ReasoningLevel(effort="medium", description="balanced"),),
        default_reasoning_level="medium",
        supports_reasoning_summaries=False,
        support_verbosity=False,
        default_verbosity=None,
        prefer_websockets=False,
        supports_parallel_tool_calls=True,
        supported_in_api=True,
        minimal_client_version=None,
        priority=0,
        available_in_plans=frozenset(),
        raw={},
    )


def _model_with_support(slug: str, *, supported_in_api: bool) -> UpstreamModel:
    return replace(_model(slug), supported_in_api=supported_in_api)


@pytest.mark.asyncio
async def test_initial_snapshot_is_none():
    registry = ModelRegistry(ttl_seconds=60.0)
    assert registry.get_snapshot() is None


@pytest.mark.asyncio
async def test_plan_types_for_model_returns_none_when_uninitialized():
    registry = ModelRegistry(ttl_seconds=60.0)
    result = registry.plan_types_for_model("some-model")
    assert result is None


def test_plan_types_for_model_uses_bootstrap_when_uninitialized():
    registry = ModelRegistry(ttl_seconds=60.0)

    assert registry.plan_types_for_model("gpt-5.6-sol") is not None
    assert registry.plan_types_for_model("gpt-5.4") == EXPECTED_CORE_MODEL_PLANS
    assert registry.plan_types_for_model("GPT-5.4") == EXPECTED_CORE_MODEL_PLANS


@pytest.mark.asyncio
async def test_plan_types_for_model_returns_empty_for_unknown_model():
    registry = ModelRegistry(ttl_seconds=60.0)
    await registry.update({"plus": [_model("model-a")]})
    result = registry.plan_types_for_model("unknown-model")
    assert result == frozenset()


@pytest.mark.asyncio
async def test_plan_types_for_model_returns_plans():
    registry = ModelRegistry(ttl_seconds=60.0)
    await registry.update(
        {
            "plus": [_model("model-a"), _model("model-b")],
            "pro": [_model("model-a"), _model("model-c")],
        }
    )

    assert registry.plan_types_for_model("model-a") == frozenset({"plus", "pro"})
    assert registry.plan_types_for_model("model-b") == frozenset({"plus"})
    assert registry.plan_types_for_model("model-c") == frozenset({"pro"})


@pytest.mark.asyncio
async def test_prefers_websockets_uses_snapshot_value():
    registry = ModelRegistry(ttl_seconds=60.0)
    preferred = replace(_model("model-ws"), prefer_websockets=True)
    await registry.update({"plus": [preferred]})

    assert registry.prefers_websockets("model-ws") is True
    assert registry.prefers_websockets("unknown-model") is False


def test_prefers_websockets_uses_bootstrap_fallback_when_uninitialized():
    registry = ModelRegistry(ttl_seconds=60.0)

    assert registry.prefers_websockets("gpt-5.6-sol") is True
    assert registry.prefers_websockets("gpt-5.5") is True
    assert registry.prefers_websockets("gpt-5.4") is True
    assert registry.prefers_websockets("gpt-5.4-2026") is True
    assert registry.prefers_websockets("gpt-5.1") is False


def test_bootstrap_models_include_current_codex_catalog():
    registry = ModelRegistry(ttl_seconds=60.0)
    models = registry.get_models_with_fallback()

    assert set(models) == BOOTSTRAP_MODEL_SLUGS

    sol = models["gpt-5.6-sol"]
    assert sol.display_name == "GPT-5.6-Sol"
    assert sol.context_window == 372_000
    assert sol.default_reasoning_level == "low"
    assert {level.effort for level in sol.supported_reasoning_levels} == {
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
    }
    assert sol.supported_in_api is True
    assert sol.raw["visibility"] == "list"

    auto_review = models["codex-auto-review"]
    assert auto_review.raw["visibility"] == "hide"
    assert auto_review.raw["max_context_window"] == 1_000_000
    assert auto_review.available_in_plans == EXPECTED_CORE_MODEL_PLANS


@pytest.mark.asyncio
async def test_update_merges_models_across_plans():
    registry = ModelRegistry(ttl_seconds=60.0)
    await registry.update(
        {
            "plus": [_model("shared"), _model("plus-only")],
            "pro": [_model("shared"), _model("pro-only")],
        }
    )

    snapshot = registry.get_snapshot()
    assert snapshot is not None
    assert set(snapshot.models.keys()) == {"shared", "plus-only", "pro-only"}
    assert snapshot.plan_models["plus"] == frozenset({"shared", "plus-only"})
    assert snapshot.plan_models["pro"] == frozenset({"shared", "pro-only"})


@pytest.mark.asyncio
async def test_partial_update_preserves_stale_plans():
    registry = ModelRegistry(ttl_seconds=60.0)

    # First full update with both plans
    await registry.update(
        {
            "plus": [_model("shared"), _model("plus-only")],
            "pro": [_model("shared"), _model("pro-only")],
        }
    )

    # Partial update: only plus succeeds, pro fails (not in per_plan_results)
    await registry.update(
        {
            "plus": [_model("shared"), _model("plus-new")],
        }
    )

    snapshot = registry.get_snapshot()
    assert snapshot is not None

    # pro-only should be preserved from previous snapshot
    assert "pro-only" in snapshot.models
    assert "pro" in snapshot.model_plans.get("pro-only", frozenset())

    # plus-only should be gone (not in new plus results)
    assert "plus-only" not in snapshot.models

    # plus-new should be present
    assert "plus-new" in snapshot.models
    assert "plus" in snapshot.model_plans["plus-new"]


def test_needs_refresh_true_initially():
    registry = ModelRegistry(ttl_seconds=60.0)
    assert registry.needs_refresh() is True


@pytest.mark.asyncio
async def test_needs_refresh_false_after_update():
    registry = ModelRegistry(ttl_seconds=60.0)
    await registry.update({"plus": [_model("a")]})
    assert registry.needs_refresh() is False


@pytest.mark.asyncio
async def test_needs_refresh_true_after_ttl(monkeypatch):
    registry = ModelRegistry(ttl_seconds=1.0)
    await registry.update({"plus": [_model("a")]})
    assert registry.needs_refresh() is False

    # Simulate time passage by adjusting fetched_at
    snapshot = registry.get_snapshot()
    assert snapshot is not None
    snapshot.fetched_at = time.monotonic() - 2.0
    assert registry.needs_refresh() is True


@pytest.mark.asyncio
async def test_empty_update_is_noop():
    registry = ModelRegistry(ttl_seconds=60.0)
    await registry.update({})
    assert registry.get_snapshot() is None


def test_ttl_must_be_positive():
    with pytest.raises(ValueError, match="positive"):
        ModelRegistry(ttl_seconds=0)
    with pytest.raises(ValueError, match="positive"):
        ModelRegistry(ttl_seconds=-1.0)


def test_is_public_model_requires_supported_in_api_true():
    public = _model_with_support("model-public", supported_in_api=True)
    hidden = _model_with_support("model-hidden", supported_in_api=False)

    assert is_public_model(public, None)
    assert not is_public_model(hidden, None)
    assert not is_public_model(hidden, {"model-hidden", "model-public"})


@pytest.mark.asyncio
async def test_plan_models_reverse_index():
    registry = ModelRegistry(ttl_seconds=60.0)
    await registry.update(
        {
            "plus": [_model("a"), _model("b")],
            "pro": [_model("b"), _model("c")],
        }
    )

    snapshot = registry.get_snapshot()
    assert snapshot is not None
    assert snapshot.plan_models["plus"] == frozenset({"a", "b"})
    assert snapshot.plan_models["pro"] == frozenset({"b", "c"})
