from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Base
from app.modules.news.repository import NewsHistoryRecord, NewsRepository
from app.modules.news.service import NewsService

pytestmark = pytest.mark.unit


@pytest.fixture
async def async_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    session = session_factory()
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_news_repository_stores_full_and_compact_versions(async_session: AsyncSession) -> None:
    repo = NewsRepository(async_session)
    now = datetime.now(timezone.utc)

    inserted = await repo.add_history_records(
        [
            NewsHistoryRecord(
                section="rumors",
                item_identity="https://x.com/example/status/1",
                semantic_signature="same claim",
                full={"headline": "完整消息", "url": "https://x.com/example/status/1"},
                compact={"headline": "浓缩消息", "item_identity": "https://x.com/example/status/1"},
                source_url="https://x.com/example/status/1",
                source_published_at="2026-04-28",
                generated_at=now,
                recorded_at=now,
            )
        ]
    )

    assert inserted == 1
    compact = await repo.list_recent_compact_items(section="rumors", since=now - timedelta(days=1))
    assert compact == [{"headline": "浓缩消息", "item_identity": "https://x.com/example/status/1"}]

    duplicate_inserted = await repo.add_history_records(
        [
            NewsHistoryRecord(
                section="rumors",
                item_identity="https://x.com/example/status/1",
                semantic_signature="same claim",
                full={"headline": "重复消息"},
                compact={"headline": "重复浓缩"},
                recorded_at=now,
            )
        ]
    )
    assert duplicate_inserted == 0


def test_news_fallback_novelty_uses_compact_history_for_reposts(tmp_path: Path) -> None:
    service = NewsService(project_root=tmp_path, cache_file=tmp_path / "news.json")
    previous = [
        {
            "item_identity": "https://x.com/source/status/1",
            "headline": "GPT-5.5 出现在 Codex 选择器",
            "summary": "多位用户传播 Codex 中出现 GPT-5.5 的截图。",
            "why_it_matters": "说明下一轮模型可能接近内部准备。",
        }
    ]
    current = [
        {
            "headline": "X 上又有人转发 GPT-5.5 Codex 选择器截图",
            "summary": "不同账号复述同一张 Codex 模型选择器截图，核心 claim 仍是 GPT-5.5 出现。",
            "why_it_matters": "说明下一轮模型可能接近内部准备。",
            "url": "https://x.com/another/status/2",
        }
    ]

    assert service._fallback_novelty_flags(section="rumors", previous=previous, current=current) == [False]


def test_company_filter_keeps_latest_candidate_when_lane_has_no_new_items(tmp_path: Path) -> None:
    service = NewsService(project_root=tmp_path, cache_file=tmp_path / "news.json")
    current = [
        {
            "company": "OpenAI",
            "headline": "OpenAI 与 Microsoft 重订合作框架",
            "sources": [
                {
                    "url": "https://openai.com/index/next-phase-of-microsoft-partnership",
                    "published_at": "2026-04-27",
                }
            ],
        },
        {
            "company": "Anthropic / Claude",
            "headline": "Claude 拿下 NEC 约 3 万员工级部署",
            "sources": [{"url": "https://www.anthropic.com/news/anthropic-nec", "published_at": "2026-04-24"}],
        },
    ]

    filtered, flags = service._filter_new_or_latest_company_items(current, [False, True])
    marked = service._mark_company_novelty(filtered, datetime.now(timezone.utc).isoformat(), flags)

    assert [item["company"] for item in filtered] == ["OpenAI", "Anthropic / Claude"]
    assert flags == [False, True]
    assert marked[0]["is_new"] is False
    assert marked[1]["is_new"] is True


def test_company_filter_keeps_only_latest_old_candidate_per_lane(tmp_path: Path) -> None:
    service = NewsService(project_root=tmp_path, cache_file=tmp_path / "news.json")
    current = [
        {
            "company": "OpenAI",
            "headline": "旧 OpenAI 动态",
            "sources": [{"url": "https://openai.com/old", "published_at": "2026-04-20"}],
        },
        {
            "company": "OpenAI",
            "headline": "较新的 OpenAI 动态",
            "sources": [{"url": "https://openai.com/newer", "published_at": "2026-04-27"}],
        },
    ]

    filtered, flags = service._filter_new_or_latest_company_items(current, [False, False])

    assert [item["headline"] for item in filtered] == ["较新的 OpenAI 动态"]
    assert flags == [False]


def test_rumor_filter_backfills_latest_old_items_to_target(tmp_path: Path) -> None:
    service = NewsService(project_root=tmp_path, cache_file=tmp_path / "news.json")
    current = [
        {"headline": "最新旧传闻", "posted_at": "2026-04-28T08:00:00Z", "url": "https://x.com/a/status/1"},
        {"headline": "新传闻", "posted_at": "2026-04-28T07:00:00Z", "url": "https://x.com/b/status/2"},
        {"headline": "较早旧传闻", "posted_at": "2026-04-28T06:00:00Z", "url": "https://x.com/c/status/3"},
    ]

    filtered, flags = service._filter_new_or_latest_rumor_items(current, [False, True, False], target_count=3)
    marked = service._mark_rumor_novelty(filtered, datetime.now(timezone.utc).isoformat(), flags)

    assert [item["headline"] for item in filtered] == ["最新旧传闻", "新传闻", "较早旧传闻"]
    assert flags == [False, True, False]
    assert [item["is_new"] for item in marked] == [False, True, False]


def test_rumor_filter_uses_latest_old_items_when_target_is_smaller(tmp_path: Path) -> None:
    service = NewsService(project_root=tmp_path, cache_file=tmp_path / "news.json")
    current = [
        {"headline": "保留的新传闻", "posted_at": "2026-04-28T07:00:00Z"},
        {"headline": "较新的旧传闻", "posted_at": "2026-04-28T08:00:00Z"},
        {"headline": "较早的旧传闻", "posted_at": "2026-04-28T06:00:00Z"},
    ]

    filtered, flags = service._filter_new_or_latest_rumor_items(current, [True, False, False], target_count=2)

    assert [item["headline"] for item in filtered] == ["保留的新传闻", "较新的旧传闻"]
    assert flags == [True, False]


def test_merge_rumors_dedupes_same_current_claim_across_sources(tmp_path: Path) -> None:
    service = NewsService(project_root=tmp_path, cache_file=tmp_path / "news.json")

    merged = service._merge_rumors(
        [
            {
                "headline": "Claude/Cursor allegedly wiped a company database in 9 seconds",
                "summary": "帖文称 Anthropic Claude 驱动的 AI coding agent Cursor 删除了 PocketOS 的生产数据库和备份。",
                "why_it_matters": "如果属实，会放大企业对 AI coding agent 权限边界和生产环境隔离的担忧。",
                "url": "https://x.com/insiderwire/status/1",
                "posted_at": "2026-04-28T06:34:00Z",
            }
        ],
        [
            {
                "headline": "Claude-powered 编码代理被曝 9 秒内删除生产库和备份",
                "summary": (
                    "Polymarket 转述称，一个 Claude-powered coding agent reportedly 删除了一家公司的生产数据库及备份。"
                ),
                "why_it_matters": (
                    "如果属实，会加剧企业对 AI coding agent 权限边界、生产环境隔离和自动化回滚机制的担忧。"
                ),
                "url": "https://x.com/polymarket/status/2",
                "posted_at": "2026-04-28T07:00:00Z",
            },
            {
                "headline": "OpenAI 新模型选择器截图在 X 上传播",
                "summary": "多位用户称 Codex 模型选择器里出现了新的 GPT 条目。",
                "why_it_matters": "这可能暗示下一轮模型发布接近准备阶段。",
                "url": "https://x.com/example/status/3",
                "posted_at": "2026-04-28T08:00:00Z",
            },
        ],
    )

    assert [item["url"] for item in merged] == [
        "https://x.com/example/status/3",
        "https://x.com/insiderwire/status/1",
    ]
