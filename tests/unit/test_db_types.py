from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.dialects import postgresql

from app.db.models import BridgeRingMember, HttpBridgeSessionAlias, HttpBridgeSessionRecord
from app.db.types import UTCDateTime


def test_utc_datetime_treats_naive_bind_values_as_utc() -> None:
    value = datetime(2026, 7, 14, 17, 0, 0)

    bound = UTCDateTime().process_bind_param(value, postgresql.dialect())

    assert bound == value.replace(tzinfo=timezone.utc)


def test_utc_datetime_normalizes_aware_bind_values_to_utc() -> None:
    value = datetime(2026, 7, 15, 1, 0, 0, tzinfo=timezone(timedelta(hours=8)))

    bound = UTCDateTime().process_bind_param(value, postgresql.dialect())

    assert bound == datetime(2026, 7, 14, 17, 0, 0, tzinfo=timezone.utc)


def test_utc_datetime_returns_naive_utc_application_values() -> None:
    value = datetime(2026, 7, 15, 1, 0, 0, tzinfo=timezone(timedelta(hours=8)))

    result = UTCDateTime().process_result_value(value, postgresql.dialect())

    assert result == datetime(2026, 7, 14, 17, 0, 0)


def test_bridge_coordination_timestamps_use_utc_datetime_boundary() -> None:
    columns = (
        BridgeRingMember.registered_at,
        BridgeRingMember.last_heartbeat_at,
        HttpBridgeSessionRecord.lease_expires_at,
        HttpBridgeSessionRecord.created_at,
        HttpBridgeSessionRecord.updated_at,
        HttpBridgeSessionRecord.last_seen_at,
        HttpBridgeSessionRecord.closed_at,
        HttpBridgeSessionAlias.created_at,
        HttpBridgeSessionAlias.updated_at,
    )

    assert all(isinstance(column.type, UTCDateTime) for column in columns)
