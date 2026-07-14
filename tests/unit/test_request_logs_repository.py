from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import ResourceClosedError

from app.db.session import SessionLocal
from app.modules.request_logs.repository import RequestLogsRepository


@pytest.mark.asyncio
async def test_bridge_latency_health_snapshot_anchors_to_latest_request_log(db_setup) -> None:
    del db_setup
    anchor_at = datetime(2026, 1, 1, 12, 2, 30)
    async with SessionLocal() as session:
        repo = RequestLogsRepository(session)
        await repo.add_log(
            account_id=None,
            request_id="req-old",
            model="gpt-5.5",
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            latency_first_token_ms=1,
            status="success",
            error_code=None,
            requested_at=anchor_at - timedelta(hours=1),
        )
        await repo.add_log(
            account_id=None,
            request_id="req-ok",
            model="gpt-5.5",
            input_tokens=1,
            output_tokens=1,
            latency_ms=1_000,
            latency_first_token_ms=1_000,
            status="success",
            error_code=None,
            requested_at=anchor_at - timedelta(minutes=2),
        )
        await repo.add_log(
            account_id=None,
            request_id="req-warning",
            model="gpt-5.5",
            input_tokens=1,
            output_tokens=1,
            latency_ms=12_000,
            latency_first_token_ms=12_000,
            status="success",
            error_code=None,
            requested_at=anchor_at - timedelta(minutes=1),
        )
        await repo.add_log(
            account_id=None,
            request_id="req-critical",
            model="gpt-5.5",
            input_tokens=1,
            output_tokens=1,
            latency_ms=30_000,
            latency_first_token_ms=30_000,
            status="success",
            error_code=None,
            requested_at=anchor_at,
        )

        snapshot = await repo.bridge_latency_health_snapshot(
            latency_window_minutes=3,
            availability_window_minutes=10,
            history_bucket_count=3,
        )

    assert snapshot.anchor_at == anchor_at
    assert snapshot.latency_first_token_p50_ms == 12_000
    assert snapshot.latency_first_token_p95_ms == 30_000
    assert snapshot.latency_first_token_p99_ms == 30_000
    assert snapshot.success_count == 3
    assert snapshot.request_count == 3
    assert snapshot.success_rate_percent == 100.0
    assert [bucket.status for bucket in snapshot.history] == ["ok", "warning", "critical"]


@pytest.mark.asyncio
async def test_add_log_ignores_closed_transaction(monkeypatch) -> None:
    async with SessionLocal() as session:
        repo = RequestLogsRepository(session)

        async def _commit_failure() -> None:
            raise ResourceClosedError("This transaction is closed")

        async def _refresh_failure(_: object) -> None:
            raise AssertionError("refresh should not be called after commit failure")

        monkeypatch.setattr(session, "commit", _commit_failure)
        monkeypatch.setattr(session, "refresh", _refresh_failure)

        log = await repo.add_log(
            account_id="acc",
            request_id="req",
            model="gpt-5.2",
            input_tokens=1000,
            output_tokens=500,
            latency_ms=1,
            status="success",
            error_code=None,
        )

        assert log.request_id == "req"
        assert log.cost_usd is not None


@pytest.mark.asyncio
async def test_request_log_persists_cache_write_tokens_and_reprices_after_model_rewrite(db_setup) -> None:
    del db_setup
    async with SessionLocal() as session:
        repo = RequestLogsRepository(session)
        log = await repo.add_log(
            account_id=None,
            request_id="req-cache-write",
            model="gpt-5.6-sol",
            input_tokens=1_000,
            output_tokens=0,
            cached_input_tokens=0,
            cache_write_tokens=1_000,
            latency_ms=1,
            status="success",
            error_code=None,
        )

        assert log.cache_write_tokens == 1_000
        assert log.cost_usd == pytest.approx(0.00625)

        assert await repo.update_model_for_request("req-cache-write", "gpt-5.6-terra") == 1
        await session.refresh(log)
        assert log.cache_write_tokens == 1_000
        assert log.cost_usd == pytest.approx(0.003125)


@pytest.mark.asyncio
async def test_request_log_distinguishes_unknown_and_zero_cache_write_tokens(db_setup) -> None:
    del db_setup
    async with SessionLocal() as session:
        repo = RequestLogsRepository(session)
        unknown = await repo.add_log(
            account_id=None,
            request_id="req-cache-write-unknown",
            model="gpt-5.6-sol",
            input_tokens=1,
            output_tokens=0,
            cache_write_tokens=None,
            latency_ms=1,
            status="success",
            error_code=None,
        )
        zero = await repo.add_log(
            account_id=None,
            request_id="req-cache-write-zero",
            model="gpt-5.6-sol",
            input_tokens=1,
            output_tokens=0,
            cache_write_tokens=0,
            latency_ms=1,
            status="success",
            error_code=None,
        )

        assert unknown.cache_write_tokens is None
        assert zero.cache_write_tokens == 0


@pytest.mark.asyncio
async def test_find_latest_account_id_for_response_id_prefers_session_then_falls_back_to_api_key_scope() -> None:
    session = AsyncMock()
    repo = RequestLogsRepository(session)
    executed_sql: list[str] = []
    returned_values = iter(
        [
            "acc_latest",
            "acc_scoped",
            "acc_session",
            None,
            "acc_scoped",
            None,
        ]
    )

    async def _execute(statement):
        executed_sql.append(str(statement))
        value = next(returned_values)
        return SimpleNamespace(scalar_one_or_none=lambda: value)

    session.execute.side_effect = _execute

    owner_any = await repo.find_latest_account_id_for_response_id(
        response_id="resp_lookup_owner",
        api_key_id=None,
    )
    owner_scoped = await repo.find_latest_account_id_for_response_id(
        response_id="resp_lookup_owner",
        api_key_id="api_key_1",
    )
    owner_session = await repo.find_latest_account_id_for_response_id(
        response_id="resp_lookup_owner",
        api_key_id="api_key_1",
        session_id="sid_terminal_a",
    )
    owner_session_fallback = await repo.find_latest_account_id_for_response_id(
        response_id="resp_lookup_owner",
        api_key_id="api_key_1",
        session_id="sid_terminal_b",
    )
    owner_missing = await repo.find_latest_account_id_for_response_id(
        response_id="resp_missing_owner",
        api_key_id=None,
    )

    assert owner_any == "acc_latest"
    assert owner_scoped == "acc_scoped"
    assert owner_session == "acc_session"
    assert owner_session_fallback == "acc_scoped"
    assert owner_missing is None
    assert "request_logs.api_key_id = :api_key_id_1" not in executed_sql[0]
    assert "request_logs.api_key_id = :api_key_id_1" in executed_sql[1]
    assert "request_logs.session_id = :session_id_1" in executed_sql[2]
    assert "request_logs.session_id = :session_id_1" in executed_sql[3]
    assert "request_logs.session_id = :session_id_1" not in executed_sql[4]


@pytest.mark.asyncio
async def test_find_latest_account_id_for_response_id_ignores_blank_response_id() -> None:
    session = AsyncMock()
    repo = RequestLogsRepository(session)

    owner = await repo.find_latest_account_id_for_response_id(
        response_id="   ",
        api_key_id="api_key_1",
        session_id="sid_terminal_a",
    )

    assert owner is None
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_find_latest_account_id_for_response_id_ignores_blank_session_id_scope() -> None:
    session = AsyncMock()
    repo = RequestLogsRepository(session)
    executed_sql: list[str] = []

    async def _execute(statement):
        executed_sql.append(str(statement))
        return SimpleNamespace(scalar_one_or_none=lambda: "acc_scoped")

    session.execute.side_effect = _execute

    owner = await repo.find_latest_account_id_for_response_id(
        response_id="resp_lookup_owner",
        api_key_id="api_key_1",
        session_id="   ",
    )

    assert owner == "acc_scoped"
    assert len(executed_sql) == 1
    assert "request_logs.session_id = :session_id_1" not in executed_sql[0]


@pytest.mark.asyncio
async def test_find_latest_account_id_for_response_id_falls_back_when_session_scope_owner_is_blank() -> None:
    session = AsyncMock()
    repo = RequestLogsRepository(session)
    executed_sql: list[str] = []
    returned_values = iter(["   ", "acc_fallback"])

    async def _execute(statement):
        executed_sql.append(str(statement))
        return SimpleNamespace(scalar_one_or_none=lambda: next(returned_values))

    session.execute.side_effect = _execute

    owner = await repo.find_latest_account_id_for_response_id(
        response_id="resp_lookup_owner",
        api_key_id="api_key_1",
        session_id="sid_terminal_a",
    )

    assert owner == "acc_fallback"
    assert len(executed_sql) == 2
    assert "request_logs.session_id = :session_id_1" in executed_sql[0]
    assert "request_logs.session_id = :session_id_1" not in executed_sql[1]
