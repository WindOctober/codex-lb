from __future__ import annotations

import gzip
import json
import logging
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Protocol, cast

from app.core.clients.proxy import ProxyResponseError
from app.core.openai.response_create import (
    _response_create_too_large_error_envelope,
    _safe_dump_slug,
    _should_dump_oversized_response_create,
    _summarize_response_create_payload,
)
from app.core.types import JsonValue

logger = logging.getLogger(__name__)


class _ResponseCreateRequestState(Protocol):
    request_id: str
    request_log_id: str | None
    response_id: str | None
    transport: str
    model: str | None
    reasoning_effort: str | None
    service_tier: str | None
    requested_service_tier: str | None
    actual_service_tier: str | None
    previous_response_id: str | None
    awaiting_response_created: bool
    replay_count: int
    request_text: str | None


def _enforce_response_create_size_limit(
    request_state: _ResponseCreateRequestState,
    *,
    warn_bytes: int,
    max_bytes: int,
    dump_dir: Path,
    largest_items_limit: int = 10,
) -> None:
    request_text = request_state.request_text
    if not request_text:
        return

    payload_size = len(request_text.encode("utf-8"))
    if payload_size > warn_bytes:
        logger.warning(
            (
                "Large response.create prepared request_id=%s request_log_id=%s "
                "transport=%s bytes=%s previous_response_id=%s"
            ),
            request_state.request_id,
            request_state.request_log_id,
            request_state.transport,
            payload_size,
            request_state.previous_response_id,
        )
    if payload_size <= max_bytes:
        return

    payload = _response_create_too_large_error_envelope(payload_size, max_bytes)
    error = payload["error"]
    _write_response_create_dump(
        request_state,
        account_id_value=None,
        error_code=cast(str, error.get("code") or "payload_too_large"),
        error_message=error.get("message"),
        log_prefix="guarded",
        dump_dir=dump_dir,
        largest_items_limit=largest_items_limit,
    )
    raise ProxyResponseError(
        413,
        payload,
        failure_phase="validation",
        failure_detail=f"response.create_bytes={payload_size}",
    )


def _maybe_dump_oversized_response_create_request(
    request_state: _ResponseCreateRequestState,
    *,
    account_id_value: str | None,
    error_code: str,
    error_message: str | None,
    dump_dir: Path,
    largest_items_limit: int = 10,
) -> None:
    if not _should_dump_oversized_response_create(error_code, error_message):
        return
    _write_response_create_dump(
        request_state,
        account_id_value=account_id_value,
        error_code=error_code,
        error_message=error_message,
        log_prefix="oversized",
        dump_dir=dump_dir,
        largest_items_limit=largest_items_limit,
    )


def _write_response_create_dump(
    request_state: _ResponseCreateRequestState,
    *,
    account_id_value: str | None,
    error_code: str,
    error_message: str | None,
    log_prefix: str,
    dump_dir: Path,
    largest_items_limit: int = 10,
) -> bool:
    request_text = request_state.request_text
    if not request_text:
        return False

    payload_bytes = request_text.encode("utf-8")
    request_sha = sha256(payload_bytes).hexdigest()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    dump_id = "-".join(
        (
            timestamp,
            _safe_dump_slug(request_state.transport, fallback="transport"),
            _safe_dump_slug(request_state.model, fallback="model"),
            _safe_dump_slug(
                request_state.request_log_id or request_state.response_id or request_state.request_id,
                fallback="request",
            ),
        )
    )
    dump_path = dump_dir / f"{dump_id}.response-create.json.gz"
    meta_path = dump_dir / f"{dump_id}.meta.json"

    meta: dict[str, JsonValue] = {
        "dump_id": dump_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "reason": {"error_code": error_code, "error_message": error_message},
        "request": {
            "account_id": account_id_value,
            "request_id": request_state.request_id,
            "request_log_id": request_state.request_log_id,
            "response_id": request_state.response_id,
            "transport": request_state.transport,
            "model": request_state.model,
            "reasoning_effort": request_state.reasoning_effort,
            "service_tier": request_state.service_tier,
            "requested_service_tier": request_state.requested_service_tier,
            "actual_service_tier": request_state.actual_service_tier,
            "previous_response_id": request_state.previous_response_id,
            "awaiting_response_created": request_state.awaiting_response_created,
            "replay_count": request_state.replay_count,
            "request_text_bytes": len(payload_bytes),
            "request_text_chars": len(request_text),
            "request_text_sha256": request_sha,
        },
        "paths": {"dump_path": str(dump_path), "meta_path": str(meta_path)},
    }

    try:
        parsed_payload = json.loads(request_text)
    except json.JSONDecodeError as exc:
        meta["parse_error"] = str(exc)
    else:
        if isinstance(parsed_payload, dict):
            meta["summary"] = _summarize_response_create_payload(
                cast(dict[str, JsonValue], parsed_payload),
                largest_items_limit=largest_items_limit,
            )
        else:
            meta["summary"] = {"payload_type": type(parsed_payload).__name__}

    try:
        dump_dir.mkdir(parents=True, exist_ok=True)
        with gzip.open(dump_path, "wt", encoding="utf-8") as handle:
            handle.write(request_text)
        meta_path.write_text(json.dumps(meta, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    except Exception:
        logger.exception(
            "Failed to dump %s response.create payload request_id=%s request_log_id=%s",
            log_prefix,
            request_state.request_id,
            request_state.request_log_id,
        )
        return False

    logger.warning(
        "Saved %s response.create dump request_id=%s request_log_id=%s dump_path=%s meta_path=%s bytes=%s",
        log_prefix,
        request_state.request_id,
        request_state.request_log_id,
        dump_path,
        meta_path,
        len(payload_bytes),
    )
    return True
