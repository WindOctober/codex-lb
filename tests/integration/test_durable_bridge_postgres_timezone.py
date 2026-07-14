from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from app.core.utils.time import to_utc_naive
from app.db.models import BridgeRingMember
from app.db.session import SessionLocal, engine
from app.modules.proxy.durable_bridge_coordinator import DurableBridgeSessionCoordinator
from app.modules.proxy.ring_membership import RingMembershipService


@pytest.mark.asyncio
async def test_postgres_durable_clocks_survive_non_utc_process_timezone(db_setup) -> None:
    del db_setup
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL test database is required")

    previous_timezone = os.environ.get("TZ")
    instance_id = f"timezone-test-{uuid4().hex}"
    session_key = f"timezone-test-{uuid4().hex}"
    claimed = None
    ring = RingMembershipService(SessionLocal)
    coordinator = DurableBridgeSessionCoordinator(SessionLocal)
    try:
        os.environ["TZ"] = "Asia/Taipei"
        time.tzset()
        started_at = datetime.now(timezone.utc)

        await ring.register(instance_id)
        claimed = await coordinator.claim_live_session(
            session_key_kind="session_header",
            session_key_value=session_key,
            api_key_id=None,
            instance_id=instance_id,
            lease_ttl_seconds=60.0,
            account_id="timezone-test-account",
            model="gpt-5.6-sol",
            service_tier=None,
            latest_turn_state=None,
            latest_response_id=None,
            allow_takeover=True,
        )

        async with SessionLocal() as session:
            member = (
                await session.execute(
                    select(BridgeRingMember).where(BridgeRingMember.instance_id == instance_id)
                )
            ).scalar_one()
            heartbeat_age_seconds = await session.scalar(
                text(
                    "SELECT EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - last_heartbeat_at)) "
                    "FROM bridge_ring_members WHERE instance_id = :instance_id"
                ),
                {"instance_id": instance_id},
            )
            lease_remaining_seconds = await session.scalar(
                text(
                    "SELECT EXTRACT(EPOCH FROM (lease_expires_at - CURRENT_TIMESTAMP)) "
                    "FROM http_bridge_sessions WHERE id = :session_id"
                ),
                {"session_id": claimed.session_id},
            )

        assert abs((to_utc_naive(member.last_heartbeat_at) - to_utc_naive(started_at)).total_seconds()) < 5
        assert heartbeat_age_seconds is not None
        assert -1 < float(heartbeat_age_seconds) < 5
        assert claimed.lease_expires_at is not None
        assert to_utc_naive(claimed.lease_expires_at) >= to_utc_naive(started_at + timedelta(seconds=55))
        assert lease_remaining_seconds is not None
        assert 50 < float(lease_remaining_seconds) <= 60
        assert claimed.lease_is_active(now=datetime.now(timezone.utc)) is True
    finally:
        if claimed is not None:
            await coordinator.release_live_session(
                session_id=claimed.session_id,
                instance_id=instance_id,
                owner_epoch=claimed.owner_epoch,
                draining=False,
            )
        await ring.unregister(instance_id)
        if previous_timezone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_timezone
        time.tzset()
