from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from app.modules.proxy import service as proxy_service
from app.modules.proxy._service.http_bridge import keys
from app.modules.proxy._service.support import _HTTPBridgeSession, _HTTPBridgeSessionKey


def test_proxy_service_reexports_required_http_bridge_key_operations() -> None:
    names = (
        "_http_bridge_turn_state_alias_key",
        "_http_bridge_previous_response_alias_key",
        "_http_bridge_soft_sharding_allowed",
        "_http_bridge_soft_shard_key",
        "_http_bridge_busy_parallel_key",
    )

    for name in names:
        assert getattr(proxy_service, name) is getattr(keys, name)


def test_http_bridge_shard_keys_preserve_family_and_strength() -> None:
    base = _HTTPBridgeSessionKey("prompt_cache", "cache", "key", strength="soft")
    soft_shard = keys._http_bridge_soft_shard_key(base, 2)
    parallel = keys._http_bridge_busy_parallel_key(soft_shard, 3)

    assert keys._http_bridge_soft_shard_index(soft_shard) == 2
    assert keys._http_bridge_busy_parallel_index(parallel) == 3
    assert keys._http_bridge_family_affinity_key(parallel) == "cache"
    assert parallel.strength == "soft"
    assert keys._http_bridge_key_strength(parallel) == "soft"


def test_http_bridge_busy_parallel_key_replaces_existing_parallel_suffix() -> None:
    base = _HTTPBridgeSessionKey("prompt_cache", "cache#codex-lb-parallel=1", None)
    replaced = keys._http_bridge_busy_parallel_key(base, 4)

    assert replaced.affinity_key == "cache#codex-lb-parallel=4"
    assert keys._http_bridge_busy_parallel_index(replaced) == 4


def test_http_bridge_alias_and_batch_keys_are_canonical() -> None:
    assert keys._http_bridge_turn_state_alias_key(" turn ", "key") == (" turn ", "key")
    assert keys._http_bridge_previous_response_alias_key(" response ", "key") == ("response", "key")

    session = cast(
        _HTTPBridgeSession,
        SimpleNamespace(
            created_at=125.0,
            request_model="gpt-5.5",
            key=_HTTPBridgeSessionKey("prompt_cache", "cache", "key"),
        ),
    )
    assert keys._http_bridge_parallel_batch_key(session, batch_window_seconds=60.0) == ("gpt-5.5", "key", 2)
