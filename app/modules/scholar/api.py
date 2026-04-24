from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse

from app.core.auth.dependencies import set_dashboard_error_format, validate_dashboard_session
from app.modules.scholar.service import ScholarService

router = APIRouter(tags=["scholar"])


def _get_scholar_service(request: Request) -> ScholarService:
    service = getattr(request.app.state, "scholar_service", None)
    if not isinstance(service, ScholarService):
        raise RuntimeError("Scholar service is not available on the application state.")
    return service


@router.get(
    "/api/scholar",
    dependencies=[Depends(validate_dashboard_session), Depends(set_dashboard_error_format)],
)
async def get_scholar(request: Request) -> dict:
    return _get_scholar_service(request).get_snapshot()


@router.post(
    "/api/scholar/refresh",
    dependencies=[Depends(validate_dashboard_session), Depends(set_dashboard_error_format)],
)
async def refresh_scholar(request: Request) -> dict[str, object]:
    service = _get_scholar_service(request)
    queued = await service.request_refresh(force=True)
    snapshot = service.get_snapshot()
    return {
        "queued": queued,
        "status": snapshot["status"],
        "refresh_in_progress": snapshot["refresh_in_progress"],
        "last_completed_at": snapshot["last_completed_at"],
    }


@router.get("/scholar-frame", include_in_schema=False, dependencies=[Depends(validate_dashboard_session)])
async def scholar_frame() -> FileResponse:
    static_dir = Path(__file__).resolve().parents[2] / "static"
    return FileResponse(static_dir / "scholar-frame.html", media_type="text/html")
