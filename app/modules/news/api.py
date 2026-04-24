from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse

from app.core.auth.dependencies import set_dashboard_error_format, validate_dashboard_session
from app.modules.news.service import NewsService

router = APIRouter(tags=["news"])


def _get_news_service(request: Request) -> NewsService:
    service = getattr(request.app.state, "news_service", None)
    if not isinstance(service, NewsService):
        raise RuntimeError("News service is not available on the application state.")
    return service


@router.get(
    "/api/news",
    dependencies=[Depends(validate_dashboard_session), Depends(set_dashboard_error_format)],
)
async def get_news(request: Request) -> dict:
    return _get_news_service(request).get_snapshot()


@router.post(
    "/api/news/refresh",
    dependencies=[Depends(validate_dashboard_session), Depends(set_dashboard_error_format)],
)
async def refresh_news(request: Request) -> dict[str, object]:
    service = _get_news_service(request)
    queued = await service.request_refresh(force=True)
    snapshot = service.get_snapshot()
    return {
        "queued": queued,
        "status": snapshot["status"],
        "refresh_in_progress": snapshot["refresh_in_progress"],
        "last_completed_at": snapshot["last_completed_at"],
    }


@router.post(
    "/api/news/mark-read",
    dependencies=[Depends(validate_dashboard_session), Depends(set_dashboard_error_format)],
)
async def mark_news_read(request: Request) -> dict[str, object]:
    marked = _get_news_service(request).mark_all_read()
    snapshot = _get_news_service(request).get_snapshot()
    return {
        "marked": marked,
        "status": snapshot["status"],
        "last_completed_at": snapshot["last_completed_at"],
    }


@router.get("/news-frame", include_in_schema=False, dependencies=[Depends(validate_dashboard_session)])
async def news_frame() -> FileResponse:
    static_dir = Path(__file__).resolve().parents[2] / "static"
    return FileResponse(static_dir / "news-frame.html", media_type="text/html")
