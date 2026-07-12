from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.auth.dependencies import set_dashboard_error_format, validate_dashboard_session
from app.modules.processes.schemas import ProcessTreesClearUnreadResponse, ProcessTreesResponse
from app.modules.processes.service import ProcessTreeService

router = APIRouter(
    prefix="/api/processes",
    tags=["processes"],
    dependencies=[Depends(validate_dashboard_session), Depends(set_dashboard_error_format)],
)


@router.get("/trees", response_model=ProcessTreesResponse)
async def get_process_trees() -> ProcessTreesResponse:
    return ProcessTreeService().list_codex_trees()


@router.post("/trees/{pid}/read", status_code=204)
async def mark_process_tree_read(pid: int) -> None:
    ProcessTreeService().mark_tree_read(pid)


@router.post("/trees/read-and-clear", response_model=ProcessTreesClearUnreadResponse)
async def mark_unread_process_trees_read_and_clear() -> ProcessTreesClearUnreadResponse:
    return ProcessTreeService().mark_unread_ended_trees_read_and_clear()
