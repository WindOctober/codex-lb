from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.core.auth.dependencies import set_dashboard_error_format, validate_dashboard_session
from app.modules.codex_reset_forecast.schemas import CodexResetForecastResponse
from app.modules.codex_reset_forecast.service import CodexResetForecastService

router = APIRouter(
    prefix="/api/codex-reset",
    tags=["codex-reset"],
    dependencies=[Depends(validate_dashboard_session), Depends(set_dashboard_error_format)],
)


def _get_service(request: Request) -> CodexResetForecastService:
    service = getattr(request.app.state, "codex_reset_forecast_service", None)
    if isinstance(service, CodexResetForecastService):
        return service
    return CodexResetForecastService()


@router.get("/forecast", response_model=CodexResetForecastResponse)
async def get_codex_reset_forecast(request: Request) -> CodexResetForecastResponse:
    return _get_service(request).get_forecast()
