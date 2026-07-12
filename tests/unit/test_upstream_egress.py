from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.core.clients.proxy as proxy_client_module
from app.core.config.settings import Settings
from app.core.egress import EgressSelection, UpstreamEgressRuntime
from app.core.openai.requests import ResponsesRequest


def test_auto_egress_switches_after_failures_and_recovers_after_cooldown() -> None:
    runtime = UpstreamEgressRuntime(
        Settings(
            upstream_egress_mode="auto",
            upstream_proxy_url="http://127.0.0.1:7890",
            upstream_egress_fail_threshold=3,
            upstream_egress_recover_threshold=3,
            upstream_egress_cooldown_seconds=60,
        )
    )

    runtime._apply_probe_result(direct_ok=False, proxy_ok=True, direct_latency_ms=4000, proxy_latency_ms=220, now=100.0)
    runtime._apply_probe_result(direct_ok=False, proxy_ok=True, direct_latency_ms=4001, proxy_latency_ms=221, now=105.0)
    assert runtime.select().route == "direct"

    runtime._apply_probe_result(direct_ok=False, proxy_ok=True, direct_latency_ms=4002, proxy_latency_ms=222, now=110.0)
    selection = runtime.select()
    assert selection.route == "proxy"
    assert selection.proxy_url == "http://127.0.0.1:7890"
    snapshot = runtime.snapshot()
    assert snapshot.direct_latency_ms == 4002
    assert snapshot.proxy_latency_ms == 222

    runtime._apply_probe_result(direct_ok=True, proxy_ok=True, now=115.0)
    runtime._apply_probe_result(direct_ok=True, proxy_ok=True, now=120.0)
    runtime._apply_probe_result(direct_ok=True, proxy_ok=True, now=125.0)
    assert runtime.select().route == "proxy"

    runtime._apply_probe_result(direct_ok=True, proxy_ok=True, now=171.0)
    assert runtime.select().route == "direct"


def test_auto_egress_requires_stable_proxy_before_failover() -> None:
    runtime = UpstreamEgressRuntime(
        Settings(
            upstream_egress_mode="auto",
            upstream_proxy_url="http://127.0.0.1:7890",
            upstream_egress_fail_threshold=3,
            upstream_egress_recover_threshold=3,
            upstream_egress_cooldown_seconds=60,
        )
    )

    runtime._apply_probe_result(direct_ok=False, proxy_ok=True, now=100.0)
    runtime._apply_probe_result(direct_ok=False, proxy_ok=False, now=105.0)
    runtime._apply_probe_result(direct_ok=False, proxy_ok=True, now=110.0)
    runtime._apply_probe_result(direct_ok=False, proxy_ok=True, now=115.0)
    assert runtime.select().route == "direct"

    runtime._apply_probe_result(direct_ok=False, proxy_ok=True, now=120.0)
    assert runtime.select().route == "proxy"


def test_auto_egress_stays_direct_without_proxy_url() -> None:
    runtime = UpstreamEgressRuntime(
        Settings(
            upstream_egress_mode="auto",
            upstream_proxy_url=None,
            upstream_egress_fail_threshold=3,
        )
    )

    runtime._apply_probe_result(direct_ok=False, proxy_ok=None, now=100.0)
    runtime._apply_probe_result(direct_ok=False, proxy_ok=None, now=105.0)
    runtime._apply_probe_result(direct_ok=False, proxy_ok=None, now=110.0)

    assert runtime.select().route == "direct"


class _FakeGetContext:
    def __init__(self, status: int) -> None:
        self._status = status

    async def __aenter__(self) -> SimpleNamespace:
        return SimpleNamespace(status=self._status)

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb


class _FakeClientSession:
    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs

    async def __aenter__(self) -> "_FakeClientSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb

    def get(self, *args, **kwargs) -> _FakeGetContext:
        del args, kwargs
        return _FakeGetContext(status=503)


@pytest.mark.asyncio
async def test_direct_probe_treats_http_response_as_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = UpstreamEgressRuntime(
        Settings(
            upstream_egress_mode="auto",
            upstream_proxy_url="http://127.0.0.1:7890",
        )
    )
    monkeypatch.setattr("app.core.egress.aiohttp.ClientSession", _FakeClientSession)

    direct_ok, direct_latency_ms = await runtime._probe(proxy_url=None)
    proxy_ok, proxy_latency_ms = await runtime._probe(proxy_url="http://127.0.0.1:7890")

    assert direct_ok is True
    assert direct_latency_ms >= 0
    assert proxy_ok is False
    assert proxy_latency_ms >= 0


class _FakeContent:
    def __init__(self) -> None:
        self._chunks = [
            b'data: {"type":"response.completed","response":{"id":"resp_1"}}\n\n',
        ]

    async def iter_chunked(self, size: int):
        del size
        for chunk in self._chunks:
            yield chunk


class _FakeResponse:
    status = 200
    headers: dict[str, str] = {}
    content = _FakeContent()


class _FakePostContext:
    async def __aenter__(self) -> _FakeResponse:
        return _FakeResponse()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb


class _FakeSession:
    def __init__(self) -> None:
        self.seen_proxy: str | None = None

    def post(self, *args, **kwargs):
        del args
        self.seen_proxy = kwargs.get("proxy")
        return _FakePostContext()


@pytest.mark.asyncio
async def test_stream_responses_uses_selected_proxy_url(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_session = _FakeSession()
    monkeypatch.setattr(
        proxy_client_module,
        "get_http_client",
        lambda: SimpleNamespace(session=fake_session),
    )
    monkeypatch.setattr(
        proxy_client_module,
        "select_upstream_egress",
        lambda: EgressSelection(route="proxy", proxy_url="http://127.0.0.1:7890"),
    )
    monkeypatch.setattr(
        proxy_client_module,
        "_resolve_stream_transport",
        lambda **kwargs: "http",
    )

    payload = ResponsesRequest(model="gpt-5", instructions="", input="hello")
    chunks = [
        chunk
        async for chunk in proxy_client_module.stream_responses(
            payload,
            {},
            "access-token",
            "account-1",
        )
    ]

    assert chunks
    assert fake_session.seen_proxy == "http://127.0.0.1:7890"
