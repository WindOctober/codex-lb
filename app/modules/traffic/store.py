from __future__ import annotations

import math
import threading
import time
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


def isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


@dataclass(slots=True)
class TrafficRequestRecord:
    internal_id: str
    request_id: str
    start_time: datetime
    end_time: datetime | None
    duration_ms: float | None
    method: str
    path: str
    host: str | None
    inferred_remote_port: str
    client_ip: str | None
    status_code: int | None = None
    error_type: str | None = None
    error_message: str | None = None
    request_bytes: int = 0
    response_bytes: int = 0
    is_streaming: bool = False
    state: str = "active"
    first_byte_ms: float | None = None
    model: str | None = None
    upstream_target: str | None = None
    provider: str | None = None

    def to_dict(self, *, now: datetime | None = None) -> dict[str, Any]:
        payload = asdict(self)
        payload["start_time"] = isoformat(self.start_time)
        payload["end_time"] = isoformat(self.end_time)
        if self.state == "active" and self.duration_ms is None:
            reference = now or utc_now()
            payload["duration_ms"] = max(0.0, (reference - self.start_time).total_seconds() * 1000.0)
        return payload


@dataclass(slots=True)
class HeartbeatRecord:
    source: str
    received_at: datetime
    received_monotonic: float
    payload: dict[str, Any]

    def is_stale(self, stale_seconds: float) -> bool:
        return (time.monotonic() - self.received_monotonic) > stale_seconds

    def age_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.received_monotonic)


def _percentiles(values: Iterable[float]) -> dict[str, float | None]:
    ordered = sorted(v for v in values if v is not None)
    if not ordered:
        return {"p50": None, "p95": None, "p99": None}

    def pick(percentile: float) -> float:
        if len(ordered) == 1:
            return round(ordered[0], 2)
        index = math.ceil((percentile / 100.0) * len(ordered)) - 1
        index = min(max(index, 0), len(ordered) - 1)
        return round(ordered[index], 2)

    return {"p50": pick(50), "p95": pick(95), "p99": pick(99)}


def _empty_port_metrics(port: str) -> dict[str, Any]:
    return {
        "port": port,
        "name": None,
        "role": None,
        "owner": None,
        "configured": False,
        "enabled": None,
        "used": None,
        "heartbeat_stale": None,
        "tunnel_healthy": None,
        "tunnel_latency_ms": None,
        "tunnel_connections": None,
        "ssh_pid": None,
        "watchdog_pid": None,
        "request_active": 0,
        "stream_active": 0,
        "request_total": 0,
        "request_errors": 0,
        "request_aborted": 0,
        "rpm": 0,
        "bytes_in_per_minute": 0,
        "bytes_out_per_minute": 0,
        "p50": None,
        "p95": None,
        "p99": None,
        "last_error": None,
        "last_seen": None,
    }


class TrafficStore:
    def __init__(self, *, ring_size: int = 2000, heartbeat_stale_seconds: float = 90.0) -> None:
        self._lock = threading.RLock()
        self._ring: deque[TrafficRequestRecord] = deque(maxlen=ring_size)
        self._active: dict[str, TrafficRequestRecord] = {}
        self._heartbeats: dict[str, HeartbeatRecord] = {}
        self._ring_size = ring_size
        self._heartbeat_stale_seconds = heartbeat_stale_seconds
        self._started_at = utc_now()
        self._started_monotonic = time.monotonic()

    def configure(self, *, ring_size: int, heartbeat_stale_seconds: float) -> None:
        with self._lock:
            if ring_size != self._ring_size:
                self._ring = deque(self._ring, maxlen=ring_size)
                self._ring_size = ring_size
            self._heartbeat_stale_seconds = heartbeat_stale_seconds

    def reset(self) -> None:
        with self._lock:
            self._ring.clear()
            self._active.clear()
            self._heartbeats.clear()
            self._started_at = utc_now()
            self._started_monotonic = time.monotonic()

    def start_request(self, record: TrafficRequestRecord) -> None:
        with self._lock:
            self._active[record.internal_id] = record

    def finish_request(self, internal_id: str, **updates: Any) -> TrafficRequestRecord | None:
        with self._lock:
            record = self._active.pop(internal_id, None)
            if record is None:
                return None
            for key, value in updates.items():
                if hasattr(record, key):
                    setattr(record, key, value)
            self._ring.append(record)
            return record

    def record_heartbeat(self, payload: dict[str, Any]) -> None:
        source = str(payload.get("source") or "unknown")
        copied = dict(payload)
        with self._lock:
            self._heartbeats[source] = HeartbeatRecord(
                source=source,
                received_at=utc_now(),
                received_monotonic=time.monotonic(),
                payload=copied,
            )

    def recent_requests(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            now = utc_now()
            records = [*self._active.values(), *self._ring]
            records.sort(key=lambda item: item.start_time, reverse=True)
            return [record.to_dict(now=now) for record in records[: max(0, limit)]]

    def ports(self) -> list[dict[str, Any]]:
        with self._lock:
            completed = list(self._ring)
            active = list(self._active.values())
            heartbeats = list(self._heartbeats.values())
        return self._build_ports(completed=completed, active=active, heartbeats=heartbeats)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            completed = list(self._ring)
            active = list(self._active.values())
            heartbeats = list(self._heartbeats.values())
            total_requests = len(completed) + len(active)
            total_errors = sum(1 for record in completed if _is_error(record))
            total_aborted = sum(1 for record in completed if record.state == "aborted")
            total_streams = sum(1 for record in completed if record.is_streaming) + sum(
                1 for record in active if record.is_streaming
            )
            latest = max((record.received_at for record in heartbeats), default=None)
            latest_hb = max(heartbeats, key=lambda record: record.received_at, default=None)
            heartbeat_stale = True if latest_hb is None else latest_hb.is_stale(self._heartbeat_stale_seconds)

        window_records = _records_in_last_minute(completed)
        latency = _percentiles(record.duration_ms for record in completed if record.duration_ms is not None)
        per_path = _group_records(completed, active, key_fn=lambda record: record.path or "/")
        per_model = _group_records(completed, active, key_fn=lambda record: record.model or "unknown")
        ports = self._build_ports(completed=completed, active=active, heartbeats=heartbeats)
        local_backend = _latest_local_backend(heartbeats, stale_seconds=self._heartbeat_stale_seconds)
        latest_heartbeat_age_seconds = latest_hb.age_seconds() if latest_hb is not None else None

        return {
            "uptime_seconds": round(max(0.0, time.monotonic() - self._started_monotonic), 3),
            "heartbeat_sources": [record.source for record in sorted(heartbeats, key=lambda item: item.source)],
            "latest_heartbeat_at": isoformat(latest),
            "latest_heartbeat_age_seconds": (
                round(latest_heartbeat_age_seconds, 3) if latest_heartbeat_age_seconds is not None else None
            ),
            "heartbeat_stale": heartbeat_stale,
            "total_requests": total_requests,
            "active_requests": len(active),
            "total_errors": total_errors,
            "total_aborted": total_aborted,
            "total_streams": total_streams,
            "active_streams": sum(1 for record in active if record.is_streaming),
            "requests_per_minute": len(window_records),
            "bytes_in_per_minute": sum(record.request_bytes for record in window_records),
            "bytes_out_per_minute": sum(record.response_bytes for record in window_records),
            "latency": latency,
            "per_port": ports,
            "per_path": per_path,
            "per_model": per_model,
            "local_backend": local_backend,
        }

    def _build_ports(
        self,
        *,
        completed: list[TrafficRequestRecord],
        active: list[TrafficRequestRecord],
        heartbeats: list[HeartbeatRecord],
    ) -> list[dict[str, Any]]:
        ports: dict[str, dict[str, Any]] = {}
        heartbeat_by_port: dict[str, tuple[HeartbeatRecord, dict[str, Any]]] = {}
        for heartbeat in heartbeats:
            for tunnel in heartbeat.payload.get("tunnels") or []:
                if not isinstance(tunnel, dict):
                    continue
                raw_port = tunnel.get("remote_port")
                if raw_port is None:
                    continue
                port = str(raw_port)
                current = heartbeat_by_port.get(port)
                if current is None or heartbeat.received_at >= current[0].received_at:
                    heartbeat_by_port[port] = (heartbeat, tunnel)

        for port, (heartbeat, tunnel) in heartbeat_by_port.items():
            item = ports.setdefault(port, _empty_port_metrics(port))
            item.update(
                {
                    "name": tunnel.get("name"),
                    "role": tunnel.get("role"),
                    "owner": tunnel.get("owner"),
                    "configured": bool(tunnel.get("configured", True)),
                    "enabled": tunnel.get("enabled"),
                    "used": tunnel.get("used"),
                    "heartbeat_stale": heartbeat.is_stale(self._heartbeat_stale_seconds),
                    "tunnel_healthy": tunnel.get("healthy"),
                    "tunnel_latency_ms": tunnel.get("latency_ms"),
                    "tunnel_connections": tunnel.get("established_connections"),
                    "ssh_pid": tunnel.get("ssh_pid"),
                    "watchdog_pid": tunnel.get("watchdog_pid"),
                    "last_error": tunnel.get("last_error"),
                    "last_seen": isoformat(heartbeat.received_at),
                }
            )

        records_by_port: dict[str, list[TrafficRequestRecord]] = defaultdict(list)
        for record in [*completed, *active]:
            records_by_port[record.inferred_remote_port or "unknown"].append(record)

        cutoff = utc_now().timestamp() - 60.0
        for port, records in records_by_port.items():
            item = ports.setdefault(port, _empty_port_metrics(port))
            active_records = [record for record in records if record.state == "active"]
            completed_records = [record for record in records if record.state != "active"]
            window_records = [
                record
                for record in completed_records
                if record.end_time is not None and record.end_time.timestamp() >= cutoff
            ]
            item.update(
                {
                    "request_active": len(active_records),
                    "stream_active": sum(1 for record in active_records if record.is_streaming),
                    "request_total": len(records),
                    "request_errors": sum(1 for record in completed_records if _is_error(record)),
                    "request_aborted": sum(1 for record in completed_records if record.state == "aborted"),
                    "rpm": len(window_records),
                    "bytes_in_per_minute": sum(record.request_bytes for record in window_records),
                    "bytes_out_per_minute": sum(record.response_bytes for record in window_records),
                    "last_seen": _latest_seen(records) or item.get("last_seen"),
                }
            )
            item.update(
                _percentiles(record.duration_ms for record in completed_records if record.duration_ms is not None)
            )
            error = next((record for record in reversed(completed_records) if _is_error(record)), None)
            if error is not None and not item.get("last_error"):
                item["last_error"] = error.error_message or error.error_type or f"HTTP {error.status_code}"

        return sorted(ports.values(), key=lambda item: _port_sort_key(str(item["port"])))


def _is_error(record: TrafficRequestRecord) -> bool:
    return record.state == "errored" or record.error_type is not None or (
        record.status_code is not None and record.status_code >= 400
    )


def _records_in_last_minute(records: list[TrafficRequestRecord]) -> list[TrafficRequestRecord]:
    cutoff = utc_now().timestamp() - 60.0
    return [record for record in records if record.end_time is not None and record.end_time.timestamp() >= cutoff]


def _group_records(
    completed: list[TrafficRequestRecord],
    active: list[TrafficRequestRecord],
    *,
    key_fn,
) -> list[dict[str, Any]]:
    groups: dict[str, list[TrafficRequestRecord]] = defaultdict(list)
    for record in [*completed, *active]:
        groups[str(key_fn(record))].append(record)
    result: list[dict[str, Any]] = []
    cutoff = utc_now().timestamp() - 60.0
    for key, records in groups.items():
        completed_records = [record for record in records if record.state != "active"]
        window_records = [
            record
            for record in completed_records
            if record.end_time is not None and record.end_time.timestamp() >= cutoff
        ]
        item = {
            "key": key,
            "request_active": sum(1 for record in records if record.state == "active"),
            "request_total": len(records),
            "request_errors": sum(1 for record in completed_records if _is_error(record)),
            "request_aborted": sum(1 for record in completed_records if record.state == "aborted"),
            "rpm": len(window_records),
            "bytes_in_per_minute": sum(record.request_bytes for record in window_records),
            "bytes_out_per_minute": sum(record.response_bytes for record in window_records),
        }
        item.update(_percentiles(record.duration_ms for record in completed_records if record.duration_ms is not None))
        result.append(item)
    return sorted(result, key=lambda item: (-int(item["request_total"]), str(item["key"])))


def _latest_seen(records: list[TrafficRequestRecord]) -> str | None:
    values = [record.end_time or record.start_time for record in records]
    if not values:
        return None
    return isoformat(max(values))


def _latest_local_backend(heartbeats: list[HeartbeatRecord], *, stale_seconds: float) -> dict[str, Any] | None:
    latest = max(heartbeats, key=lambda record: record.received_at, default=None)
    if latest is None:
        return None
    local_backend = latest.payload.get("local_backend")
    if not isinstance(local_backend, dict):
        return None
    result = dict(local_backend)
    result["source"] = latest.source
    result["last_seen"] = isoformat(latest.received_at)
    result["heartbeat_stale"] = latest.is_stale(stale_seconds)
    result["heartbeat_age_seconds"] = round(latest.age_seconds(), 3)
    return result


def _port_sort_key(port: str) -> tuple[int, int | str]:
    try:
        return (0, int(port))
    except ValueError:
        return (1, port)


_traffic_store = TrafficStore()


def get_traffic_store() -> TrafficStore:
    return _traffic_store
