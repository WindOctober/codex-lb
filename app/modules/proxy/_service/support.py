from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, TypeVar, cast

import anyio

from app.core.balancer import DEFAULT_ROUTING_STRATEGY, PERMANENT_FAILURE_CODES, RoutingStrategy
from app.core.balancer.types import ClassifiedFailure, UpstreamError
from app.core.clients.proxy_websocket import UpstreamResponsesWebSocket, UpstreamWebSocketMessage
from app.core.openai.models import OpenAIEvent, ResponseUsage, normalize_response_usage
from app.core.types import JsonValue
from app.db.models import Account, DashboardSettings, StickySessionKind
from app.modules.api_keys.service import ApiKeyData, ApiKeyUsageCharge, ApiKeyUsageReservationData
from app.modules.proxy.account_concurrency import AccountModelConcurrencyLease
from app.modules.proxy.load_balancer import AccountSelection
from app.modules.proxy.work_admission import AdmissionLease

logger = logging.getLogger("app.modules.proxy.service")

_REQUEST_TRANSPORT_HTTP = "http"
_REQUEST_TRANSPORT_WEBSOCKET = "websocket"
_ACCOUNT_RECOVERY_RETRY_CODES = frozenset(
    {
        "rate_limit_exceeded",
        "usage_limit_reached",
        "insufficient_quota",
        "usage_not_included",
        "quota_exceeded",
        *PERMANENT_FAILURE_CODES.keys(),
    }
)
_HARD_HTTP_BRIDGE_AFFINITY_KINDS = frozenset({"turn_state_header", "session_header"})
_HTTP_BRIDGE_SOFT_SHARD_MARKER = "#codex-lb-shard="
_HTTP_BRIDGE_BUSY_PARALLEL_MARKER = "#codex-lb-parallel="
_ACCOUNT_SELECTION_RECOVERY_HEARTBEAT_SECONDS = 10.0
_ACCOUNT_SELECTION_RECOVERABLE_WAIT_CODE = "account_rate_limit_wait"
_ACCOUNT_SELECTION_RECOVERABLE_WAIT_REASON = "waiting for account rate-limit recovery"
_WEBSOCKET_MAX_ACCOUNT_ATTEMPTS = 3
_MAX_TRANSIENT_SAME_ACCOUNT_RETRIES = 3
_CleanupResultT = TypeVar("_CleanupResultT")
_DiscardedAccountingResolution = Literal["fallback"] | Callable[[], Awaitable[None]]


async def _await_cancelled_task(
    task: asyncio.Task[Any],
    *,
    timeout_seconds: float = 1.0,
    label: str,
) -> bool:
    task.cancel()
    done, _pending = await asyncio.wait({task}, timeout=timeout_seconds)
    if task not in done:
        logger.warning("Timed out waiting for %s cancellation", label)
        return False
    try:
        await task
    except asyncio.CancelledError:
        return True
    return True


def _track_existing_background_task(
    tasks: set[asyncio.Task[None]],
    task: asyncio.Task[Any],
    *,
    label: str,
) -> None:
    """Retain and observe an already-created task until it really finishes."""
    tracked_task = cast(asyncio.Task[None], task)
    if tracked_task in tasks:
        return
    tasks.add(tracked_task)

    def consume_result(completed: asyncio.Task[Any]) -> None:
        tasks.discard(tracked_task)
        try:
            completed.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("Tracked %s failed after detachment", label, exc_info=True)

    task.add_done_callback(consume_result)


async def _await_operation_before_hard_timeout(
    operation: Awaitable[_CleanupResultT],
    *,
    timeout_seconds: float,
    tasks: set[asyncio.Task[None]],
    label: str,
    late_result_cleanup: Callable[[_CleanupResultT], Awaitable[None]] | None = None,
    late_completion_cleanup: Callable[[], Awaitable[None]] | None = None,
    on_detach: Callable[[], None] | None = None,
) -> _CleanupResultT:
    """Observe an operation for a bounded time without waiting for cancellation."""
    if timeout_seconds <= 0:
        if inspect.iscoroutine(operation):
            operation.close()
        raise TimeoutError
    task = asyncio.ensure_future(operation)

    def retain_late_operation() -> None:
        if on_detach is not None:
            on_detach()
        task.cancel()
        if late_result_cleanup is None and late_completion_cleanup is None:
            _track_existing_background_task(tasks, task, label=label)
            return

        async def reconcile() -> None:
            try:
                result = await task
            except asyncio.CancelledError:
                pass
            else:
                if late_result_cleanup is not None:
                    await late_result_cleanup(result)
            finally:
                if late_completion_cleanup is not None:
                    await late_completion_cleanup()

        _schedule_tracked_background_task(
            tasks,
            reconcile(),
            name=f"hard-timeout-reconcile-{time.monotonic_ns()}",
            label=f"late {label} reconciliation",
        )

    try:
        done, _pending = await asyncio.wait({task}, timeout=timeout_seconds)
    except BaseException:
        retain_late_operation()
        if not task.done():
            pass
        else:
            try:
                task.result()
            except (asyncio.CancelledError, Exception):
                pass
        raise
    if task not in done:
        retain_late_operation()
        raise TimeoutError
    return task.result()


async def _await_shielded_cleanup(
    cleanup: Awaitable[_CleanupResultT],
    *,
    label: str,
) -> _CleanupResultT:
    cleanup_task = asyncio.ensure_future(cleanup)
    try:
        return await asyncio.shield(cleanup_task)
    except asyncio.CancelledError as cancellation:
        with anyio.CancelScope(shield=True):
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    if cleanup_task.done():
                        break
        if cleanup_task.cancelled():
            raise cancellation
        try:
            cleanup_task.result()
        except Exception:
            logger.exception("%s failed after caller cancellation", label)
        raise cancellation


def _schedule_tracked_background_task(
    tasks: set[asyncio.Task[None]],
    operation: Awaitable[None],
    *,
    name: str,
    label: str,
) -> None:
    async def run() -> None:
        try:
            await operation
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Background %s failed", label, exc_info=True)

    task = asyncio.create_task(run(), name=name)
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def _close_tracked_background_tasks(
    tasks: set[asyncio.Task[None]],
    *,
    label: str,
    timeout_seconds: float = 5.0,
) -> None:
    cancelled_once: set[asyncio.Task[None]] = set()
    for _round in range(2):
        tasks.difference_update(task for task in tuple(tasks) if task.done())
        active = tuple(task for task in tasks if not task.done())
        if not active:
            return
        _done, pending = await asyncio.wait(active, timeout=timeout_seconds)
        if not pending:
            continue
        newly_pending = tuple(task for task in pending if task not in cancelled_once)
        if not newly_pending:
            continue
        logger.warning("Timed out waiting for %s; cancelling %s task(s)", label, len(newly_pending))
        for task in newly_pending:
            cancelled_once.add(task)
            task.cancel()
        _cancelled, still_pending = await asyncio.wait(newly_pending, timeout=timeout_seconds)
        if still_pending:
            logger.error(
                "Hard timeout waiting for %s cancellation; leaving %s task(s) tracked",
                label,
                len(still_pending),
            )
    tasks.difference_update(task for task in tuple(tasks) if task.done())
    remaining = sum(not task.done() for task in tasks)
    if remaining:
        logger.error("Tracked %s shutdown ended with %s unfinished task(s)", label, remaining)


def _is_recoverable_account_selection_wait(selection: AccountSelection) -> bool:
    return selection.account is None and (
        selection.error_code == _ACCOUNT_SELECTION_RECOVERABLE_WAIT_CODE or selection.retry_after_seconds is not None
    )


def _account_selection_wait_retry_after_seconds(selection: AccountSelection, remaining: float) -> float | None:
    if not _is_recoverable_account_selection_wait(selection):
        return None
    if selection.retry_after_seconds is None:
        return min(_ACCOUNT_SELECTION_RECOVERY_HEARTBEAT_SECONDS, remaining)
    return min(_ACCOUNT_SELECTION_RECOVERY_HEARTBEAT_SECONDS, remaining, max(0.0, selection.retry_after_seconds))


def _account_selection_wait_sleep_seconds(selection: AccountSelection, remaining: float) -> float:
    sleep_seconds = min(1.0, remaining)
    if selection.retry_after_seconds is not None and selection.retry_after_seconds > 0:
        sleep_seconds = min(sleep_seconds, selection.retry_after_seconds)
    if sleep_seconds <= 0:
        return 0.0
    if remaining >= 0.05:
        return max(0.05, sleep_seconds)
    return remaining


def _routing_strategy(settings: DashboardSettings) -> RoutingStrategy:
    value = settings.routing_strategy or DEFAULT_ROUTING_STRATEGY
    if value == "primary_drain":
        return "primary_drain"
    if value == "high_waterline":
        return "high_waterline"
    if value == "capacity_weighted":
        return "capacity_weighted"
    if value == "usage_weighted":
        return "usage_weighted"
    return DEFAULT_ROUTING_STRATEGY


def _header_value_case_insensitive(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def _headers_with_authorization(headers: Mapping[str, str], authorization: str | None) -> dict[str, str]:
    merged = dict(headers)
    if authorization is not None and _header_value_case_insensitive(merged, "authorization") is None:
        merged["Authorization"] = authorization
    return merged


@dataclass(frozen=True, slots=True)
class _AffinityPolicy:
    key: str | None = None
    kind: StickySessionKind | None = None
    reallocate_sticky: bool = False
    max_age_seconds: int | None = None
    required_upstream_wire_api: str | None = None


def _merge_affinity_required_wire_api(
    current: _AffinityPolicy,
    incoming: _AffinityPolicy,
) -> _AffinityPolicy:
    required_wire_api = incoming.required_upstream_wire_api or current.required_upstream_wire_api
    if required_wire_api == current.required_upstream_wire_api:
        return current
    return _AffinityPolicy(
        key=current.key,
        kind=current.kind,
        reallocate_sticky=current.reallocate_sticky,
        max_age_seconds=current.max_age_seconds,
        required_upstream_wire_api=required_wire_api,
    )


@dataclass(frozen=True, slots=True)
class _DurableAccountBinding:
    account_id: str | None
    supports_request_model: bool


class _RetryableStreamError(Exception):
    def __init__(self, code: str, error: UpstreamError) -> None:
        super().__init__(code)
        self.code = code
        self.error = error


class _TransientStreamError(Exception):
    """Transient upstream error; retry on the same account first."""

    def __init__(self, code: str, error: UpstreamError) -> None:
        super().__init__(code)
        self.code = code
        self.error = error


class _TerminalStreamError(Exception):
    def __init__(self, code: str, error: UpstreamError) -> None:
        super().__init__(code)
        self.code = code
        self.error = error


@dataclass
class _StreamSettlement:
    """Populated by one stream attempt and consumed by retry settlement."""

    status: str = "success"
    model: str = ""
    service_tier: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    usage_charges: tuple[ApiKeyUsageCharge, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
    error: UpstreamError | None = None
    account_health_error: bool = False
    record_success: bool = True
    late_usage_reconciliation: asyncio.Future[_StreamSettlement | None] | None = None


@dataclass(slots=True)
class _CompactReservationOwnership:
    """Mutable handoff marker between the HTTP route and compact runtime."""

    transferred: bool = False


def _stream_settlement_has_authoritative_usage(settlement: _StreamSettlement) -> bool:
    return bool(settlement.usage_charges) or any(
        value is not None
        for value in (
            settlement.input_tokens,
            settlement.output_tokens,
            settlement.cached_input_tokens,
            settlement.cache_write_tokens,
        )
    )


def _usage_charge_from_response_usage(
    usage: ResponseUsage | None,
    *,
    model: str,
    service_tier: str | None,
) -> ApiKeyUsageCharge | None:
    normalized = normalize_response_usage(usage)
    if normalized is None:
        return None
    return ApiKeyUsageCharge(
        model=model,
        input_tokens=normalized.input_tokens,
        output_tokens=normalized.output_tokens,
        cached_input_tokens=normalized.cached_input_tokens,
        cache_write_tokens=normalized.cache_write_tokens,
        service_tier=service_tier,
    )


def _apply_usage_charges_to_settlement(
    settlement: _StreamSettlement,
    charges: Sequence[ApiKeyUsageCharge],
) -> bool:
    if not charges:
        return False
    settlement.input_tokens = sum(charge.input_tokens for charge in charges)
    settlement.output_tokens = sum(charge.output_tokens for charge in charges)
    settlement.cached_input_tokens = sum(charge.cached_input_tokens for charge in charges)
    settlement.cache_write_tokens = sum(charge.cache_write_tokens for charge in charges)
    settlement.usage_charges = tuple(charges)
    return True


def _stream_settlement_error_payload(settlement: _StreamSettlement) -> UpstreamError:
    if settlement.error is not None:
        return settlement.error
    payload: UpstreamError = {}
    payload["message"] = settlement.error_message or "Upstream error"
    return payload


def _should_penalize_stream_error(code: str | None) -> bool:
    return code is not None and code in _ACCOUNT_RECOVERY_RETRY_CODES


def _is_account_neutral_error_code(code: str | None) -> bool:
    return code in {"not_implemented", "proxy_overloaded", "proxy_unavailable", "unsupported_transport"}


@dataclass
class _WebSocketRequestState:
    request_id: str
    model: str | None
    service_tier: str | None
    reasoning_effort: str | None
    api_key_reservation: ApiKeyUsageReservationData | None
    started_at: float
    request_budget_seconds: float | None = None
    request_deadline_at: float | None = None
    latency_first_token_ms: int | None = None
    request_log_id: str | None = None
    requested_service_tier: str | None = None
    actual_service_tier: str | None = None
    response_id: str | None = None
    awaiting_response_created: bool = False
    event_queue: asyncio.Queue[str | None] | None = None
    transport: str = _REQUEST_TRANSPORT_WEBSOCKET
    api_key: ApiKeyData | None = None
    request_text: str | None = None
    replay_count: int = 0
    skip_request_log: bool = False
    previous_response_id: str | None = None
    session_id: str | None = None
    proxy_injected_previous_response_id: bool = False
    fresh_upstream_request_text: str | None = None
    # Set only when this body is a safe pre-injection form that can be replayed
    # as a fresh turn without silently discarding conversation context.
    fresh_upstream_request_is_retry_safe: bool = False
    request_stage: str = "first_turn"
    preferred_account_id: str | None = None
    error_code_override: str | None = None
    error_message_override: str | None = None
    error_type_override: str | None = None
    error_param_override: str | None = None
    error_http_status_override: int | None = None
    response_create_gate_acquired: bool = False
    response_create_gate: asyncio.Semaphore | None = None
    response_create_admission: AdmissionLease | None = None
    account_model_concurrency: AccountModelConcurrencyLease | None = None
    affinity_policy: _AffinityPolicy = field(default_factory=_AffinityPolicy)
    input_item_count: int = 0
    input_full_fingerprint: str | None = None
    account_capacity_waiting: bool = False
    account_capacity_wait_reason: str | None = None
    account_capacity_wait_started_at: float | None = None
    account_capacity_wait_retry_after_seconds: float | None = None
    http_bridge_local_owner_lookup_started_at: float | None = None
    http_bridge_local_owner_lookup_completed_at: float | None = None
    http_bridge_previous_owner_resolve_started_at: float | None = None
    http_bridge_previous_owner_resolve_completed_at: float | None = None
    http_bridge_get_or_create_started_at: float | None = None
    http_bridge_get_or_create_completed_at: float | None = None
    http_bridge_submit_started_at: float | None = None
    http_bridge_gate_wait_started_at: float | None = None
    http_bridge_gate_acquired_at: float | None = None
    http_bridge_admission_acquired_at: float | None = None
    websocket_send_started_at: float | None = None
    websocket_send_completed_at: float | None = None
    http_bridge_send_started_at: float | None = None
    http_bridge_send_completed_at: float | None = None
    http_bridge_upstream_first_event_at: float | None = None
    http_bridge_upstream_first_event_type: str | None = None
    usage_charges: list[ApiKeyUsageCharge] = field(default_factory=list)
    discarded_accounting_resolution_future: asyncio.Future[
        _DiscardedAccountingResolution
    ] | None = None
    http_bridge_upstream_first_text_at: float | None = None
    http_bridge_downstream_first_event_at: float | None = None
    http_bridge_downstream_first_text_at: float | None = None
    http_bridge_latency_breakdown_logged: bool = False


@dataclass(slots=True)
class _DiscardedRequestAccounting:
    request_state: _WebSocketRequestState
    api_key_reservation: ApiKeyUsageReservationData | None
    resolution_future: asyncio.Future[_DiscardedAccountingResolution] | None = None


def _anonymous_terminal_candidate_count(
    pending_requests: Sequence[_WebSocketRequestState],
    *,
    discarded_response_ids: set[str],
    discarded_request_accounting: Mapping[str, _DiscardedRequestAccounting],
    anonymous_discarded_request_accounting: Mapping[str, _DiscardedRequestAccounting],
) -> int:
    """Count distinct requests that could own an identity-free terminal."""
    request_identities = {id(request_state) for request_state in pending_requests}
    request_identities.update(
        id(accounting.request_state) for accounting in discarded_request_accounting.values()
    )
    request_identities.update(
        id(accounting.request_state)
        for accounting in anonymous_discarded_request_accounting.values()
    )
    orphaned_response_ids = discarded_response_ids.difference(discarded_request_accounting)
    return len(request_identities) + len(orphaned_response_ids)


@dataclass(frozen=True, slots=True)
class _HTTPBridgeSessionKey:
    affinity_kind: str
    affinity_key: str
    api_key_id: str | None
    strength: Literal["hard", "soft"] | None = None

    def __post_init__(self) -> None:
        strength = self.strength
        if strength is None:
            strength = "hard" if self.affinity_kind in _HARD_HTTP_BRIDGE_AFFINITY_KINDS else "soft"
        object.__setattr__(self, "strength", strength)


@dataclass(frozen=True, slots=True)
class _HTTPBridgeOwnerForward:
    owner_instance: str
    owner_endpoint: str
    key: _HTTPBridgeSessionKey


@dataclass(slots=True)
class _HTTPBridgeSession:
    key: _HTTPBridgeSessionKey
    headers: dict[str, str]
    affinity: _AffinityPolicy
    request_model: str | None
    account: Account
    upstream: UpstreamResponsesWebSocket
    upstream_control: _WebSocketUpstreamControl
    pending_requests: deque[_WebSocketRequestState]
    pending_lock: anyio.Lock
    response_create_gate: asyncio.Semaphore | None
    queued_request_count: int
    last_used_at: float
    idle_ttl_seconds: float
    created_at: float = field(default_factory=time.monotonic)
    api_key: ApiKeyData | None = None
    codex_session: bool = False
    prewarmed: bool = False
    prewarm_lock: anyio.Lock | None = None
    upstream_turn_state: str | None = None
    downstream_turn_state: str | None = None
    downstream_turn_state_aliases: set[str] = field(default_factory=set)
    previous_response_ids: set[str] = field(default_factory=set)
    discarded_response_ids: set[str] = field(default_factory=set)
    discarded_request_accounting: dict[str, _DiscardedRequestAccounting] = field(default_factory=dict)
    anonymous_discarded_request_accounting: dict[str, _DiscardedRequestAccounting] = field(
        default_factory=dict
    )
    discarded_accounting_reconciliation_owned: bool = False
    pending_changed: asyncio.Event = field(default_factory=asyncio.Event)
    last_completed_input_count: int = 0
    last_completed_response_id: str | None = None
    last_completed_input_prefix_fingerprint: str | None = None
    durable_session_id: str | None = None
    durable_owner_epoch: int | None = None
    durable_lease_expires_at: datetime | None = None
    durable_ownership_lost: bool = False
    durable_ownership_retirement_scheduled: bool = False
    durable_renew_after: float = 0.0
    durable_renew_task: asyncio.Task[None] | None = None
    durable_renew_lock: anyio.Lock = field(default_factory=anyio.Lock)
    upstream_reader: asyncio.Task[None] | None = None
    defer_reader_start: bool = False
    detached_upstream_receive: asyncio.Task[UpstreamWebSocketMessage] | None = None
    upstream_close_owned: bool = False
    upstream_reconnect_count: int = 0
    account_model_session_lease: AccountModelConcurrencyLease | None = None
    submit_lease_count: int = 0
    client_kind: Literal["batch", "interactive", "unknown"] = "unknown"
    closed: bool = False
    lifecycle_lock: anyio.Lock = field(default_factory=anyio.Lock)


@dataclass(slots=True)
class _WebSocketUpstreamControl:
    reconnect_requested: bool = False
    retire_ambiguous_transport: bool = False
    suppress_downstream_event: bool = False
    replay_request_state: _WebSocketRequestState | None = None
    downstream_texts: list[str] | None = None
    discarded_response_ids: set[str] = field(default_factory=set)
    discarded_request_accounting: dict[str, _DiscardedRequestAccounting] = field(default_factory=dict)
    anonymous_discarded_request_accounting: dict[str, _DiscardedRequestAccounting] = field(
        default_factory=dict
    )
    detached_receive_pending: bool = False
    discarded_accounting_sealed: bool = False
    upstream_close_owned: bool = False


@dataclass(frozen=True, slots=True)
class _HTTPBridgeExpiryResult:
    expired_requests: tuple[_WebSocketRequestState, ...]
    retire_ambiguous_transport: bool = False


@dataclass(slots=True)
class _DownstreamWebSocketActivity:
    last_activity_at: float = field(default_factory=time.monotonic)

    def mark(self) -> None:
        self.last_activity_at = time.monotonic()


@dataclass(slots=True)
class _PreparedWebSocketRequest:
    text_data: str
    request_state: _WebSocketRequestState
    affinity_policy: _AffinityPolicy


@dataclass(frozen=True, slots=True)
class _WebSocketReceiveTimeout:
    timeout_seconds: float
    error_code: str
    error_message: str
    fail_all_pending: bool = False
    response_created_request_ids: frozenset[str] = frozenset()
    response_created_request_tokens: frozenset[tuple[str, float]] = frozenset()


def _event_type_from_payload(event: OpenAIEvent | None, payload: dict[str, JsonValue] | None) -> str | None:
    if event is not None:
        return event.type
    if payload is None:
        return None
    payload_type = payload.get("type")
    return payload_type if isinstance(payload_type, str) else None


def _release_websocket_response_create_gate(
    request_state: _WebSocketRequestState,
    response_create_gate: asyncio.Semaphore | None,
) -> None:
    if request_state.response_create_admission is not None:
        request_state.response_create_admission.release()
        request_state.response_create_admission = None
    request_state.awaiting_response_created = False
    request_state.response_create_gate = None
    if response_create_gate is None:
        request_state.response_create_gate_acquired = False
        return
    if not request_state.response_create_gate_acquired:
        return
    request_state.response_create_gate_acquired = False
    response_create_gate.release()


def _supported_optional_kwargs(
    func: Callable[..., Any],
    optional_kwargs: Mapping[str, Any],
    required_kwargs: Mapping[str, Any],
) -> dict[str, Any]:
    kwargs = dict(required_kwargs)
    kwargs.update(optional_kwargs)
    if not optional_kwargs:
        return kwargs
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return kwargs
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
        return kwargs
    for name in optional_kwargs:
        if name not in signature.parameters:
            kwargs.pop(name, None)
    return kwargs


async def _call_with_supported_optional_kwargs(
    func: Callable[..., Awaitable[Any]],
    /,
    *args: Any,
    optional_kwargs: Mapping[str, Any],
    **required_kwargs: Any,
) -> Any:
    return await func(
        *args,
        **_supported_optional_kwargs(func, optional_kwargs, required_kwargs),
    )


def _should_retry_http_bridge_on_different_account(failure: ClassifiedFailure) -> bool:
    return failure["failure_class"] in {"rate_limit", "quota"} or failure["error_code"] == "server_is_overloaded"
