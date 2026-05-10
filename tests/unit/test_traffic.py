from __future__ import annotations

import time
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from httpx import ASGITransport, AsyncClient

from app.modules.traffic import api as traffic_api
from app.modules.traffic.middleware import TrafficMetricsMiddleware, infer_remote_port
from app.modules.traffic.store import TrafficRequestRecord, TrafficStore, utc_now


@pytest.mark.unit
def test_infer_remote_port_priority() -> None:
    assert infer_remote_port({"host": "127.0.0.1:32455"}) == "32455"
    assert infer_remote_port({"host": "example.test", "x-forwarded-host": "inner-server:32456"}) == "32456"
    assert infer_remote_port({"host": "example.test", "x-codex-lb-port": "32457"}) == "32457"
    assert infer_remote_port({"host": "example.test"}) == "unknown"


@pytest.mark.unit
def test_heartbeat_accept_cache_and_stale() -> None:
    store = TrafficStore(heartbeat_stale_seconds=0.01)
    store.record_heartbeat(_heartbeat_payload(remote_port=32455, used=True))

    summary = store.summary()
    assert summary["heartbeat_sources"] == ["windows-admin"]
    assert summary["heartbeat_stale"] is False

    time.sleep(0.02)
    assert store.summary()["heartbeat_stale"] is True


@pytest.mark.unit
def test_configured_but_unused_port_is_in_ports() -> None:
    store = TrafficStore()
    store.record_heartbeat(_heartbeat_payload(remote_port=32456, used=False, healthy=True))

    ports = store.ports()
    assert ports[0]["port"] == "32456"
    assert ports[0]["configured"] is True
    assert ports[0]["used"] is False
    assert ports[0]["request_total"] == 0


@pytest.mark.unit
def test_store_completed_and_aborted_states() -> None:
    store = TrafficStore()
    completed = _record("completed", port="32455")
    aborted = _record("aborted", port="32456")
    store.start_request(completed)
    store.finish_request(completed.internal_id, state="completed", end_time=utc_now(), duration_ms=12, status_code=200)
    store.start_request(aborted)
    store.finish_request(aborted.internal_id, state="aborted", end_time=utc_now(), duration_ms=7, status_code=200)

    summary = store.summary()
    assert summary["total_requests"] == 2
    assert summary["total_aborted"] == 1
    by_port = {item["port"]: item for item in summary["per_port"]}
    assert by_port["32455"]["request_total"] == 1
    assert by_port["32456"]["request_aborted"] == 1


@pytest.mark.unit
async def test_middleware_records_completed_request_and_model() -> None:
    app, store = _metrics_app()

    @app.post("/v1/responses")
    async def response(request: Request) -> JSONResponse:
        await request.json()
        return JSONResponse({"ok": True})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:32455") as client:
        response = await client.post("/v1/responses", json={"model": "gpt-5.5"})

    assert response.status_code == 200
    recent = store.recent_requests(limit=1)[0]
    assert recent["state"] == "completed"
    assert recent["inferred_remote_port"] == "32455"
    assert recent["model"] == "gpt-5.5"
    assert recent["request_bytes"] > 0
    assert recent["response_bytes"] > 0


@pytest.mark.unit
async def test_streaming_response_counting_does_not_break_stream() -> None:
    app, store = _metrics_app()

    async def chunks():
        yield b"a"
        yield b"b"

    @app.get("/v1/stream")
    async def stream() -> StreamingResponse:
        return StreamingResponse(chunks(), media_type="text/event-stream")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:32455") as client:
        response = await client.get("/v1/stream")

    assert response.content == b"ab"
    recent = store.recent_requests(limit=1)[0]
    assert recent["state"] == "completed"
    assert recent["is_streaming"] is True
    assert recent["response_bytes"] == 2


@pytest.mark.unit
async def test_traffic_api_summary_shape_and_heartbeat() -> None:
    traffic_api.get_traffic_store().reset()
    app = FastAPI()
    app.include_router(traffic_api.router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:2455") as client:
        ping = await client.get("/__traffic/ping")
        heartbeat = await client.post("/__traffic/tunnel-heartbeat", json=_heartbeat_payload(remote_port=32455))
        summary = await client.get("/__traffic/summary")
        ports = await client.get("/__traffic/ports")
        requests = await client.get("/__traffic/requests?limit=1")

    assert ping.status_code == 204
    assert heartbeat.json() == {"ok": True}
    body = summary.json()
    assert {"uptime_seconds", "heartbeat_sources", "per_port", "per_path", "per_model", "local_backend"} <= set(body)
    assert ports.json()["ports"][0]["port"] == "32455"
    assert "requests" in requests.json()


def _metrics_app() -> tuple[FastAPI, TrafficStore]:
    store = TrafficStore()
    app = FastAPI()
    app.add_middleware(TrafficMetricsMiddleware, store=store)
    return app, store


def _record(state: str, *, port: str) -> TrafficRequestRecord:
    return TrafficRequestRecord(
        internal_id=uuid4().hex,
        request_id=uuid4().hex,
        start_time=utc_now(),
        end_time=None,
        duration_ms=None,
        method="POST",
        path="/v1/responses",
        host=f"127.0.0.1:{port}",
        inferred_remote_port=port,
        client_ip="127.0.0.1",
        state=state,
    )


def _heartbeat_payload(*, remote_port: int, used: bool = True, healthy: bool = True) -> dict:
    return {
        "source": "windows-admin",
        "timestamp": "2026-05-09T18:30:00+08:00",
        "collector": {"pid": 1234, "mode": "watch"},
        "local_backend": {
            "host": "127.0.0.1",
            "port": 2455,
            "name": "codex-lb",
            "healthy": True,
            "latency_ms": 42,
            "established_connections": 12,
            "process": {"pid": 35964, "name": "Code", "cpu_seconds": 23.469, "working_set_mb": 336.5},
        },
        "tunnels": [
            {
                "remote_host": "inner-server",
                "remote_port": remote_port,
                "name": "batch",
                "role": "batch",
                "owner": "collector",
                "configured": True,
                "enabled": True,
                "used": used,
                "local_target": "127.0.0.1:2455",
                "healthy": healthy,
                "latency_ms": 120,
                "ssh_pid": 45304,
                "watchdog_pid": 55796,
                "established_connections": 3,
                "last_error": None,
            }
        ],
    }
