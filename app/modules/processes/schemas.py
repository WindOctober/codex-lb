from __future__ import annotations

from datetime import datetime

from app.modules.shared.schemas import DashboardModel


class ProcessTreeNode(DashboardModel):
    pid: int
    ppid: int
    pgid: int
    sid: int
    state: str
    status_label: str
    started_at: datetime | None = None
    elapsed_seconds: int | None = None
    command_name: str
    command_kind: str
    display_command: str
    role: str
    task_label: str | None = None
    lifecycle_status: str = "running"
    ended_at: datetime | None = None
    read_at: datetime | None = None
    cwd: str | None = None
    repo_path: str | None = None
    children: list[ProcessTreeNode]


class ProcessTreesResponse(DashboardModel):
    collected_at: datetime
    poll_interval_seconds: int
    total_trees: int
    total_processes: int
    trees: list[ProcessTreeNode]


class ProcessTreesClearUnreadResponse(DashboardModel):
    cleared_count: int
