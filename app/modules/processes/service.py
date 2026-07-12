from __future__ import annotations

import os
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from app.modules.processes.schemas import ProcessTreeNode, ProcessTreesClearUnreadResponse, ProcessTreesResponse

PROCESS_POLL_INTERVAL_SECONDS: Final = 10
ENDED_TREE_RETENTION_SECONDS: Final = 5 * 60
DOCKER_LINEAGE_CACHE_SECONDS: Final = 5
PROC_ROOT: Final = Path("/proc")
DOCKER_CONTAINER_ID_PATTERN: Final = re.compile(r"(?:docker[-/]|cri-containerd-)?([0-9a-f]{64})(?:\.scope)?")
DOCKER_LAUNCHER_PID_PATTERN: Final = re.compile(r"-(?P<pid>[1-9][0-9]*)-[^-]+$")

STATE_LABELS: Final = {
    "R": "Active",
    "S": "Running",
    "D": "I/O wait",
    "Z": "Zombie",
    "T": "Stopped",
    "t": "Tracing",
    "I": "Running",
}


@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    ppid: int
    pgid: int
    sid: int
    state: str
    started_at: datetime | None
    elapsed_seconds: int | None
    command_name: str
    args: tuple[str, ...]
    cwd: str | None
    container_id: str | None = None


@dataclass(frozen=True)
class ProcessDescription:
    command_kind: str
    display_command: str
    role: str
    task_label: str | None
    repo_path: str | None


@dataclass
class RetainedProcessTree:
    tree: ProcessTreeNode
    ended_at: datetime | None = None
    read_at: datetime | None = None


_PROCESS_TREE_CACHE: dict[int, RetainedProcessTree] = {}


@dataclass(frozen=True)
class DockerContainerLineage:
    container_id: str
    name: str
    launcher_pid: int | None


@dataclass
class DockerLineageCache:
    expires_at: datetime
    lineage_by_container_id: dict[str, DockerContainerLineage]


_DOCKER_LINEAGE_CACHE: DockerLineageCache | None = None


class ProcessTreeService:
    def list_codex_trees(self) -> ProcessTreesResponse:
        collected_at = datetime.now(UTC)
        snapshots = _scan_processes(collected_at)
        docker_lineage_by_container_id = _docker_lineage_by_container_id(snapshots, collected_at)
        children_by_parent = _build_child_index(snapshots, docker_lineage_by_container_id)
        root_pids = _find_codex_root_pids(snapshots, docker_lineage_by_container_id)
        trees = [_build_node(pid, snapshots, children_by_parent) for pid in root_pids if pid in snapshots]
        trees = _merge_retained_trees(trees, collected_at)
        total_processes = sum(_count_nodes(tree) for tree in trees)
        return ProcessTreesResponse(
            collected_at=collected_at,
            poll_interval_seconds=PROCESS_POLL_INTERVAL_SECONDS,
            total_trees=len(trees),
            total_processes=total_processes,
            trees=trees,
        )

    def mark_tree_read(self, pid: int) -> bool:
        retained = _PROCESS_TREE_CACHE.get(pid)
        if retained is None or retained.ended_at is None:
            return False
        read_at = retained.read_at or datetime.now(UTC)
        retained.read_at = read_at
        retained.tree = _with_lifecycle(
            retained.tree,
            lifecycle_status="ended",
            ended_at=retained.ended_at,
            read_at=read_at,
        )
        return True

    def mark_unread_ended_trees_read_and_clear(self) -> ProcessTreesClearUnreadResponse:
        cleared_count = 0
        for pid, retained in list(_PROCESS_TREE_CACHE.items()):
            if retained.ended_at is None or retained.read_at is not None:
                continue
            retained.read_at = datetime.now(UTC)
            del _PROCESS_TREE_CACHE[pid]
            cleared_count += 1
        return ProcessTreesClearUnreadResponse(cleared_count=cleared_count)


def _merge_retained_trees(live_trees: list[ProcessTreeNode], now: datetime) -> list[ProcessTreeNode]:
    live_pids = {tree.pid for tree in live_trees}
    for tree in live_trees:
        _PROCESS_TREE_CACHE[tree.pid] = RetainedProcessTree(
            tree=_with_lifecycle(tree, lifecycle_status="running", ended_at=None, read_at=None),
        )

    for pid, retained in list(_PROCESS_TREE_CACHE.items()):
        if pid in live_pids:
            continue
        if retained.ended_at is None:
            retained.ended_at = now
            retained.tree = _with_lifecycle(
                retained.tree,
                lifecycle_status="ended",
                ended_at=now,
                read_at=None,
            )
        if retained.read_at is not None and (now - retained.read_at).total_seconds() >= ENDED_TREE_RETENTION_SECONDS:
            del _PROCESS_TREE_CACHE[pid]

    retained_ended = [
        retained.tree
        for pid, retained in _PROCESS_TREE_CACHE.items()
        if pid not in live_pids and retained.ended_at is not None
    ]
    return live_trees + sorted(retained_ended, key=lambda tree: tree.ended_at or now)


def _with_lifecycle(
    node: ProcessTreeNode,
    *,
    lifecycle_status: str,
    ended_at: datetime | None,
    read_at: datetime | None,
) -> ProcessTreeNode:
    is_ended = lifecycle_status == "ended"
    return node.model_copy(
        update={
            "lifecycle_status": lifecycle_status,
            "ended_at": ended_at,
            "read_at": read_at,
            "state": "X" if is_ended else node.state,
            "status_label": "Ended" if is_ended else node.status_label,
            "children": [
                _with_lifecycle(
                    child,
                    lifecycle_status=lifecycle_status,
                    ended_at=ended_at,
                    read_at=read_at,
                )
                for child in node.children
            ],
        }
    )


def _reset_process_tree_cache_for_tests() -> None:
    _PROCESS_TREE_CACHE.clear()
    global _DOCKER_LINEAGE_CACHE
    _DOCKER_LINEAGE_CACHE = None


def _scan_processes(now: datetime) -> dict[int, ProcessSnapshot]:
    boot_time = _read_boot_time()
    clock_ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    snapshots: dict[int, ProcessSnapshot] = {}
    for entry in PROC_ROOT.iterdir():
        if not entry.name.isdecimal():
            continue
        snapshot = _read_process_snapshot(entry, now=now, boot_time=boot_time, clock_ticks=clock_ticks)
        if snapshot is not None:
            snapshots[snapshot.pid] = snapshot
    return snapshots


def _read_boot_time() -> int | None:
    try:
        with (PROC_ROOT / "stat").open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("btime "):
                    return int(line.split()[1])
    except (OSError, ValueError):
        return None
    return None


def _read_process_snapshot(
    proc_dir: Path,
    *,
    now: datetime,
    boot_time: int | None,
    clock_ticks: int,
) -> ProcessSnapshot | None:
    pid = int(proc_dir.name)
    try:
        stat_text = (proc_dir / "stat").read_text(encoding="utf-8")
        stat = _parse_stat(pid, stat_text)
    except (OSError, ValueError):
        return None

    args = _read_cmdline(proc_dir)
    command_name = _command_name(args=args, comm=stat.command_name)
    cwd = _read_cwd(proc_dir)
    container_id = _read_container_id(proc_dir)
    started_at = _started_at(boot_time=boot_time, start_ticks=stat.start_ticks, clock_ticks=clock_ticks)
    elapsed_seconds = None if started_at is None else max(0, int((now - started_at).total_seconds()))
    return ProcessSnapshot(
        pid=pid,
        ppid=stat.ppid,
        pgid=stat.pgid,
        sid=stat.sid,
        state=stat.state,
        started_at=started_at,
        elapsed_seconds=elapsed_seconds,
        command_name=command_name,
        args=args,
        cwd=cwd,
        container_id=container_id,
    )


@dataclass(frozen=True)
class _StatFields:
    command_name: str
    state: str
    ppid: int
    pgid: int
    sid: int
    start_ticks: int


def _parse_stat(pid: int, stat_text: str) -> _StatFields:
    prefix = f"{pid} ("
    if not stat_text.startswith(prefix):
        raise ValueError("unexpected stat prefix")
    command_end = stat_text.rfind(")")
    if command_end < len(prefix):
        raise ValueError("unexpected stat command")
    command_name = stat_text[len(prefix) : command_end]
    fields = stat_text[command_end + 2 :].split()
    if len(fields) <= 19:
        raise ValueError("stat payload is too short")
    return _StatFields(
        command_name=command_name,
        state=fields[0],
        ppid=int(fields[1]),
        pgid=int(fields[2]),
        sid=int(fields[3]),
        start_ticks=int(fields[19]),
    )


def _read_cmdline(proc_dir: Path) -> tuple[str, ...]:
    try:
        raw = (proc_dir / "cmdline").read_bytes()
    except OSError:
        return ()
    return tuple(part.decode("utf-8", errors="replace") for part in raw.rstrip(b"\0").split(b"\0") if part)


def _read_cwd(proc_dir: Path) -> str | None:
    try:
        return os.readlink(proc_dir / "cwd")
    except OSError:
        return None


def _read_container_id(proc_dir: Path) -> str | None:
    try:
        cgroup = (proc_dir / "cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    for match in DOCKER_CONTAINER_ID_PATTERN.finditer(cgroup):
        container_id = match.group(1)
        if len(container_id) == 64:
            return container_id
    return None


def _started_at(*, boot_time: int | None, start_ticks: int, clock_ticks: int) -> datetime | None:
    if boot_time is None or clock_ticks <= 0:
        return None
    return datetime.fromtimestamp(boot_time + (start_ticks / clock_ticks), tz=UTC)


def _command_name(*, args: tuple[str, ...], comm: str) -> str:
    if args:
        return Path(args[0]).name
    return comm


def _docker_lineage_by_container_id(
    snapshots: dict[int, ProcessSnapshot],
    now: datetime,
) -> dict[str, DockerContainerLineage]:
    container_ids = {process.container_id for process in snapshots.values() if process.container_id}
    if not container_ids:
        return {}

    global _DOCKER_LINEAGE_CACHE
    if _DOCKER_LINEAGE_CACHE is not None and _DOCKER_LINEAGE_CACHE.expires_at > now:
        return {
            container_id: lineage
            for container_id, lineage in _DOCKER_LINEAGE_CACHE.lineage_by_container_id.items()
            if container_id in container_ids
        }

    lineage_by_container_id = _read_docker_lineage(container_ids)
    _DOCKER_LINEAGE_CACHE = DockerLineageCache(
        expires_at=now + timedelta(seconds=DOCKER_LINEAGE_CACHE_SECONDS),
        lineage_by_container_id=lineage_by_container_id,
    )
    return lineage_by_container_id


def _read_docker_lineage(container_ids: set[str]) -> dict[str, DockerContainerLineage]:
    try:
        completed = subprocess.run(
            ["docker", "ps", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if completed.returncode != 0:
        return {}

    lineage_by_container_id: dict[str, DockerContainerLineage] = {}
    for line in completed.stdout.splitlines():
        container_id, separator, name = line.partition("\t")
        if not separator or container_id not in container_ids:
            continue
        lineage_by_container_id[container_id] = DockerContainerLineage(
            container_id=container_id,
            name=name,
            launcher_pid=_extract_launcher_pid_from_container_name(name),
        )
    return lineage_by_container_id


def _extract_launcher_pid_from_container_name(name: str) -> int | None:
    match = DOCKER_LAUNCHER_PID_PATTERN.search(name)
    if match is None:
        return None
    try:
        return int(match.group("pid"))
    except ValueError:
        return None


def _build_child_index(
    snapshots: dict[int, ProcessSnapshot],
    docker_lineage_by_container_id: dict[str, DockerContainerLineage],
) -> dict[int, list[int]]:
    children_by_parent: dict[int, list[int]] = defaultdict(list)
    for process in snapshots.values():
        children_by_parent[process.ppid].append(process.pid)
    _add_docker_lineage_edges(children_by_parent, snapshots, docker_lineage_by_container_id)
    for children in children_by_parent.values():
        children[:] = sorted(set(children))
    return children_by_parent


def _add_docker_lineage_edges(
    children_by_parent: dict[int, list[int]],
    snapshots: dict[int, ProcessSnapshot],
    docker_lineage_by_container_id: dict[str, DockerContainerLineage],
) -> None:
    container_root_pids_by_id = _container_root_pids_by_id(snapshots)
    for container_id, container_root_pids in container_root_pids_by_id.items():
        lineage = docker_lineage_by_container_id.get(container_id)
        if lineage is None:
            continue
        launcher_pid = _verified_docker_launcher_pid(lineage, snapshots)
        if launcher_pid is None:
            continue
        children_by_parent[launcher_pid].extend(container_root_pids)


def _container_root_pids_by_id(snapshots: dict[int, ProcessSnapshot]) -> dict[str, list[int]]:
    root_pids_by_container_id: dict[str, list[int]] = defaultdict(list)
    for process in snapshots.values():
        if process.container_id is None:
            continue
        parent = snapshots.get(process.ppid)
        if parent is not None and parent.container_id == process.container_id:
            continue
        root_pids_by_container_id[process.container_id].append(process.pid)
    return root_pids_by_container_id


def _find_codex_root_pids(
    snapshots: dict[int, ProcessSnapshot],
    docker_lineage_by_container_id: dict[str, DockerContainerLineage],
) -> list[int]:
    root_pids: set[int] = set()
    docker_launcher_pids = _verified_docker_launcher_pids(docker_lineage_by_container_id, snapshots)
    for process in snapshots.values():
        if not _is_codex_process(process):
            continue
        if process.pid in docker_launcher_pids:
            root_pids.add(_ancestor_root_pid(process.pid, snapshots))
            continue
        docker_root_pid = _docker_lineage_root_pid(process, snapshots, docker_lineage_by_container_id)
        if docker_root_pid is not None:
            root_pids.add(docker_root_pid)
            continue
        if _has_codex_ancestor(process, snapshots):
            continue
        root_pids.add(process.pid)
    return sorted(root_pids, key=lambda pid: snapshots[pid].started_at or datetime.min.replace(tzinfo=UTC))


def _has_codex_ancestor(
    process: ProcessSnapshot,
    snapshots: dict[int, ProcessSnapshot],
) -> bool:
    parent_pid = process.ppid
    seen: set[int] = set()
    while parent_pid not in seen:
        seen.add(parent_pid)
        parent = snapshots.get(parent_pid)
        if parent is None:
            return False
        if _is_codex_process(parent):
            return True
        if parent.ppid == parent.pid:
            return False
        parent_pid = parent.ppid
    return False


def _docker_lineage_root_pid(
    process: ProcessSnapshot,
    snapshots: dict[int, ProcessSnapshot],
    docker_lineage_by_container_id: dict[str, DockerContainerLineage],
) -> int | None:
    if process.container_id is None:
        return None
    lineage = docker_lineage_by_container_id.get(process.container_id)
    if lineage is None:
        return None
    launcher_pid = _verified_docker_launcher_pid(lineage, snapshots)
    if launcher_pid is None:
        return None
    return _ancestor_root_pid(launcher_pid, snapshots)


def _verified_docker_launcher_pid(
    lineage: DockerContainerLineage,
    snapshots: dict[int, ProcessSnapshot],
) -> int | None:
    docker_run_pid = _live_docker_run_pid(lineage, snapshots)
    if docker_run_pid is not None:
        return docker_run_pid

    launcher_pid = lineage.launcher_pid
    if launcher_pid is None or launcher_pid <= 1:
        return None
    launcher = snapshots.get(launcher_pid)
    if launcher is None or not _is_known_container_launcher(launcher, lineage.name):
        return None
    return launcher_pid


def _verified_docker_launcher_pids(
    docker_lineage_by_container_id: dict[str, DockerContainerLineage],
    snapshots: dict[int, ProcessSnapshot],
) -> set[int]:
    return {
        launcher_pid
        for lineage in docker_lineage_by_container_id.values()
        if (launcher_pid := _verified_docker_launcher_pid(lineage, snapshots)) is not None
    }


def _live_docker_run_pid(
    lineage: DockerContainerLineage,
    snapshots: dict[int, ProcessSnapshot],
) -> int | None:
    matching_pids = [
        process.pid for process in snapshots.values() if _is_docker_run_for_container(process, lineage.name)
    ]
    return min(matching_pids) if matching_pids else None


def _is_docker_run_for_container(process: ProcessSnapshot, container_name: str) -> bool:
    args = process.args
    if not args or Path(args[0]).name != "docker" or "run" not in args:
        return False
    for index, arg in enumerate(args):
        if arg == "--name" and index + 1 < len(args) and args[index + 1] == container_name:
            return True
        if arg == f"--name={container_name}":
            return True
    return False


def _is_known_container_launcher(process: ProcessSnapshot, container_name: str) -> bool:
    joined = " ".join(process.args).lower()
    if "run_codex_assertion_docker.sh" in joined:
        return True
    return _is_docker_run_for_container(process, container_name)


def _ancestor_root_pid(pid: int, snapshots: dict[int, ProcessSnapshot]) -> int:
    current = pid
    seen: set[int] = set()
    while current not in seen:
        seen.add(current)
        process = snapshots.get(current)
        if process is None:
            break
        parent = snapshots.get(process.ppid)
        if parent is None or process.ppid == 1:
            return current
        if _is_ancestor_boundary(parent, snapshots):
            return current
        current = process.ppid
    return pid


def _is_ancestor_boundary(process: ProcessSnapshot, snapshots: dict[int, ProcessSnapshot]) -> bool:
    if _is_sshd_boundary(process) or _is_vscode_remote_boundary(process):
        return True
    if not _is_shell_boundary_candidate(process):
        return False
    parent = snapshots.get(process.ppid)
    return parent is not None and (_is_sshd_boundary(parent) or _is_vscode_remote_boundary(parent))


def _is_sshd_boundary(process: ProcessSnapshot) -> bool:
    command_name = process.command_name.lower()
    joined = " ".join(process.args).lower()
    return command_name == "sshd" or joined.startswith("sshd:")


def _is_vscode_remote_boundary(process: ProcessSnapshot) -> bool:
    command_name = process.command_name.lower()
    joined = " ".join(process.args).lower()
    if command_name.startswith("code-") and "command-shell" in joined:
        return True
    return ".vscode-server" in joined and (
        "bootstrap-fork" in joined or "command-shell" in joined or "server-main.js" in joined
    )


def _is_shell_boundary_candidate(process: ProcessSnapshot) -> bool:
    return process.command_name.lower() in {"bash", "sh", "zsh", "fish"}


def _build_node(
    pid: int,
    snapshots: dict[int, ProcessSnapshot],
    children_by_parent: dict[int, list[int]],
) -> ProcessTreeNode:
    process = snapshots[pid]
    description = _describe_process(process)
    return ProcessTreeNode(
        pid=process.pid,
        ppid=process.ppid,
        pgid=process.pgid,
        sid=process.sid,
        state=process.state,
        status_label=STATE_LABELS.get(process.state, process.state),
        started_at=process.started_at,
        elapsed_seconds=process.elapsed_seconds,
        command_name=process.command_name,
        command_kind=description.command_kind,
        display_command=description.display_command,
        role=description.role,
        task_label=description.task_label,
        cwd=process.cwd,
        repo_path=description.repo_path,
        children=[
            _build_node(child_pid, snapshots, children_by_parent)
            for child_pid in children_by_parent.get(pid, [])
            if child_pid in snapshots
        ],
    )


def _count_nodes(node: ProcessTreeNode) -> int:
    return 1 + sum(_count_nodes(child) for child in node.children)


def _is_codex_process(process: ProcessSnapshot) -> bool:
    args = process.args
    joined = " ".join(args).lower()
    command_name = process.command_name.lower()
    if command_name == "codex":
        return True
    if _arg_contains(args, "/codex") and (" exec" in joined or "app-server" in joined):
        return True
    if "codex-cli" in joined or "@openai/codex" in joined:
        return True
    return False


def _describe_process(process: ProcessSnapshot) -> ProcessDescription:
    args = process.args
    repo_path = _extract_repo_path(process)
    model = _flag_value(args, "-m", "--model")
    output_path = _flag_value(args, "-o", "--output-last-message")

    if _is_codex_app_server(process):
        return ProcessDescription(
            command_kind="codex_app_server",
            display_command="codex app-server",
            role="Vscode Codex APP",
            task_label="Vscode Codex APP",
            repo_path=repo_path,
        )

    if _is_codex_exec(process):
        command_parts = ["codex exec"]
        if repo_path:
            command_parts.append(f"-C {_safe_path_label(repo_path)}")
        if model:
            command_parts.append(f"-m {model}")
        if output_path:
            command_parts.append(f"-o {_safe_path_label(output_path)}")
        return ProcessDescription(
            command_kind="codex_exec",
            display_command=" ".join(command_parts),
            role="Codex exec root",
            task_label=_codex_exec_task_label(repo_path=repo_path, output_path=output_path),
            repo_path=repo_path,
        )

    command_kind, role = _classify_child_process(process)
    return ProcessDescription(
        command_kind=command_kind,
        display_command=_safe_display_command(process),
        role=role,
        task_label=role,
        repo_path=repo_path,
    )


def _is_codex_app_server(process: ProcessSnapshot) -> bool:
    return "app-server" in process.args


def _is_codex_exec(process: ProcessSnapshot) -> bool:
    return "exec" in process.args or " codex exec" in f" {' '.join(process.args).lower()}"


def _classify_child_process(process: ProcessSnapshot) -> tuple[str, str]:
    joined = " ".join(process.args).lower()
    command_name = process.command_name.lower()
    if "run_assertion_batch.py" in joined:
        return "workflow_runner", "Schwarz assertion batch"
    if "run_codex_assertion_docker.sh" in joined:
        return "docker_launcher", "Schwarz Docker worker launcher"
    if "run_codex_assertion.sh" in joined:
        return "docker_worker", "Schwarz Docker worker"
    if command_name in {"containerd-shim", "containerd-shim-runc-v2"}:
        return "container_runtime", "Container runtime"
    if command_name == "runuser":
        return "user_switch", "Worker user switch"
    if "url-fetcher" in joined:
        return "mcp_server", "URL fetcher MCP"
    if "google" in joined and "sheet" in joined:
        return "mcp_server", "Google Sheets MCP"
    if "notion" in joined:
        return "mcp_server", "Notion MCP"
    if "mcp" in joined:
        return "mcp_server", "MCP server"
    if command_name in {"bash", "sh", "zsh", "fish"}:
        return "shell", "Shell"
    if command_name in {"node", "bun", "npm", "npx"}:
        return "node", "Node runtime"
    if command_name in {"python", "python3"}:
        return "python", "Python runtime"
    return "process", process.command_name


def _extract_repo_path(process: ProcessSnapshot) -> str | None:
    explicit_path = _flag_value(process.args, "-C", "--cd")
    if explicit_path:
        return explicit_path
    return process.cwd


def _flag_value(args: tuple[str, ...], short_flag: str, long_flag: str) -> str | None:
    for index, arg in enumerate(args):
        if arg in {short_flag, long_flag} and index + 1 < len(args):
            return args[index + 1]
        for flag in (short_flag, long_flag):
            prefix = f"{flag}="
            if arg.startswith(prefix):
                return arg[len(prefix) :]
    return None


def _codex_exec_task_label(*, repo_path: str | None, output_path: str | None) -> str:
    if output_path:
        output = Path(output_path)
        run_name = output.parent.name if output.name else output.name
        if run_name and run_name not in {".", ""}:
            return f"Codex exec: {run_name}"
    if repo_path:
        return f"Codex exec in {Path(repo_path).name}"
    return "Codex exec"


def _safe_display_command(process: ProcessSnapshot) -> str:
    if not process.args:
        return process.command_name
    executable = Path(process.args[0]).name
    script = _first_non_sensitive_script(process.args[1:])
    if script:
        return f"{executable} {script}"
    return executable


def _first_non_sensitive_script(args: tuple[str, ...]) -> str | None:
    for arg in args:
        if not arg or arg.startswith("-"):
            continue
        if _looks_sensitive(arg):
            continue
        if not _looks_like_script_path(arg):
            continue
        name = Path(arg).name
        if name:
            return name
    return None


def _looks_like_script_path(value: str) -> bool:
    suffixes = (".cjs", ".js", ".mjs", ".py", ".sh", ".ts")
    return "/" in value or value.endswith(suffixes)


def _safe_path_label(path: str) -> str:
    if _looks_sensitive(path):
        return "[redacted]"
    return path


def _looks_sensitive(value: str) -> bool:
    lowered = value.lower()
    sensitive_markers = (
        "authorization",
        "bearer ",
        "x-access-token",
        "api_key",
        "apikey",
        "token=",
        "password",
        "secret",
    )
    return any(marker in lowered for marker in sensitive_markers)


def _arg_contains(args: tuple[str, ...], needle: str) -> bool:
    return any(needle in arg.lower() for arg in args)
