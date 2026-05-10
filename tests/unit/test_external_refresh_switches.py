from __future__ import annotations

import pytest

from app.core.config.settings import get_settings
from app.modules.news.service import NewsService, build_news_service
from app.modules.scholar.service import ScholarService, build_scholar_service

pytestmark = pytest.mark.unit


def test_external_refresh_switches_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "CODEX_LB_API_PROVIDER_MODEL_REFRESH_ENABLED",
        "CODEX_LB_MODEL_REGISTRY_ENABLED",
        "CODEX_LB_NEWS_REFRESH_ENABLED",
        "CODEX_LB_TRENDRADAR_REFRESH_ENABLED",
        "CODEX_LB_SCHOLAR_REFRESH_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.api_provider_model_refresh_enabled is False
    assert settings.model_registry_enabled is False
    assert settings.news_refresh_enabled is False
    assert settings.trendradar_refresh_enabled is False
    assert settings.scholar_refresh_enabled is False


def test_build_services_respect_external_refresh_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_LB_NEWS_REFRESH_ENABLED", "true")
    monkeypatch.setenv("CODEX_LB_TRENDRADAR_REFRESH_ENABLED", "true")
    monkeypatch.setenv("CODEX_LB_SCHOLAR_REFRESH_ENABLED", "true")
    get_settings.cache_clear()

    news_service = build_news_service()
    scholar_service = build_scholar_service()

    assert news_service._refresh_enabled is True
    assert news_service._trendradar_refresh_enabled is True
    assert scholar_service._refresh_enabled is True


@pytest.mark.asyncio
async def test_disabled_news_refresh_does_not_queue_external_work(tmp_path) -> None:
    service = NewsService(
        project_root=tmp_path,
        cache_file=tmp_path / "news.json",
        refresh_enabled=False,
        trendradar_refresh_enabled=False,
    )

    assert await service.request_refresh(force=True) is False
    assert await service.request_trendradar_refresh(force=True) is False
    snapshot = service.get_snapshot()

    assert snapshot["refresh_enabled"] is False
    assert snapshot["trendradar_refresh_enabled"] is False
    assert service._refresh_task is None
    assert service._trendradar_refresh_task is None


@pytest.mark.asyncio
async def test_disabled_scholar_refresh_does_not_queue_external_work(tmp_path) -> None:
    service = ScholarService(
        project_root=tmp_path,
        cache_file=tmp_path / "scholar.json",
        topic_cache_file=tmp_path / "topics.json",
        refresh_enabled=False,
    )

    assert await service.request_refresh(force=True) is False
    snapshot = service.get_snapshot()

    assert snapshot["refresh_enabled"] is False
    assert service._refresh_task is None
