from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import NewsItem


@dataclass(frozen=True, slots=True)
class NewsHistoryRecord:
    section: str
    item_identity: str
    semantic_signature: str | None
    full: dict[str, Any]
    compact: dict[str, Any]
    source_url: str | None = None
    source_published_at: str | None = None
    generated_at: datetime | None = None
    recorded_at: datetime | None = None


class NewsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_recent_compact_items(
        self,
        *,
        section: str,
        since: datetime,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        stmt = (
            select(NewsItem.compact_json)
            .where(NewsItem.section == section)
            .where(NewsItem.recorded_at >= since)
            .order_by(NewsItem.recorded_at.desc(), NewsItem.id.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        items: list[dict[str, Any]] = []
        for raw in result.scalars().all():
            try:
                payload = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                items.append(payload)
        return items

    async def add_history_records(self, records: Iterable[NewsHistoryRecord]) -> int:
        normalized = [record for record in records if record.section and record.item_identity]
        if not normalized:
            return 0

        existing: set[tuple[str, str]] = set()
        for section in {record.section for record in normalized}:
            identities = [record.item_identity for record in normalized if record.section == section]
            stmt = (
                select(NewsItem.section, NewsItem.item_identity)
                .where(NewsItem.section == section)
                .where(NewsItem.item_identity.in_(identities))
            )
            result = await self._session.execute(stmt)
            existing.update((row.section, row.item_identity) for row in result.all())

        inserted = 0
        for record in normalized:
            key = (record.section, record.item_identity)
            if key in existing:
                continue
            values: dict[str, Any] = {
                "section": record.section,
                "item_identity": record.item_identity,
                "semantic_signature": record.semantic_signature,
                "full_json": json.dumps(record.full, ensure_ascii=False, sort_keys=True),
                "compact_json": json.dumps(record.compact, ensure_ascii=False, sort_keys=True),
                "source_url": record.source_url,
                "source_published_at": record.source_published_at,
                "generated_at": record.generated_at,
            }
            if record.recorded_at is not None:
                values["recorded_at"] = record.recorded_at
            self._session.add(NewsItem(**values))
            existing.add(key)
            inserted += 1
        await self._session.commit()
        return inserted
