from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.modules.processes import service as process_service
from app.modules.processes.service import ProcessSnapshot, ProcessTreeService, _describe_process


def test_codex_exec_description_uses_safe_summary_without_prompt() -> None:
    process = ProcessSnapshot(
        pid=123,
        ppid=1,
        pgid=123,
        sid=123,
        state="S",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        elapsed_seconds=60,
        command_name="codex",
        args=(
            "codex",
            "exec",
            "-C",
            "/work/repo",
            "-m",
            "gpt-5",
            "-o",
            "/tmp/runs/fix-bug/last-message.md",
            "very long user prompt with bearer token=secret",
        ),
        cwd="/work/repo",
    )

    description = _describe_process(process)

    assert description.command_kind == "codex_exec"
    assert description.repo_path == "/work/repo"
    assert description.task_label == "Codex exec: fix-bug"
    assert description.display_command == "codex exec -C /work/repo -m gpt-5 -o /tmp/runs/fix-bug/last-message.md"
    assert "very long user prompt" not in description.display_command
    assert "secret" not in description.display_command


def test_child_process_description_redacts_sensitive_script_argument() -> None:
    process = ProcessSnapshot(
        pid=124,
        ppid=123,
        pgid=123,
        sid=123,
        state="S",
        started_at=None,
        elapsed_seconds=None,
        command_name="node",
        args=("node", "--token=secret", "/tmp/url-fetcher-mcp/index.js"),
        cwd="/tmp/url-fetcher-mcp",
    )

    description = _describe_process(process)

    assert description.command_kind == "mcp_server"
    assert description.role == "URL fetcher MCP"
    assert description.display_command == "node index.js"
    assert "secret" not in description.display_command


def test_ended_tree_is_retained_until_read_retention_expires(monkeypatch) -> None:
    process_service._reset_process_tree_cache_for_tests()
    assert process_service.ENDED_TREE_RETENTION_SECONDS == 5 * 60

    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    snapshot = ProcessSnapshot(
        pid=321,
        ppid=1,
        pgid=321,
        sid=321,
        state="S",
        started_at=started_at,
        elapsed_seconds=10,
        command_name="codex",
        args=("codex", "exec", "-C", "/work/repo"),
        cwd="/work/repo",
    )
    monkeypatch.setattr(process_service, "_scan_processes", lambda now: {321: snapshot})

    live = ProcessTreeService().list_codex_trees()

    assert [tree.pid for tree in live.trees] == [321]
    assert live.trees[0].lifecycle_status == "running"

    monkeypatch.setattr(process_service, "_scan_processes", lambda now: {})

    ended = ProcessTreeService().list_codex_trees()

    assert [tree.pid for tree in ended.trees] == [321]
    assert ended.trees[0].lifecycle_status == "ended"
    assert ended.trees[0].status_label == "Ended"
    assert ended.trees[0].ended_at is not None
    assert ended.trees[0].read_at is None

    assert ProcessTreeService().mark_tree_read(321) is True
    read = ProcessTreeService().list_codex_trees()
    assert read.trees[0].read_at is not None

    retained = process_service._PROCESS_TREE_CACHE[321]
    assert retained.read_at is not None
    retained.read_at = retained.read_at - timedelta(seconds=process_service.ENDED_TREE_RETENTION_SECONDS - 10)

    still_retained = ProcessTreeService().list_codex_trees()

    assert [tree.pid for tree in still_retained.trees] == [321]

    retained = process_service._PROCESS_TREE_CACHE[321]
    assert retained.read_at is not None
    retained.read_at = retained.read_at - timedelta(seconds=11)

    expired = ProcessTreeService().list_codex_trees()

    assert expired.trees == []
    process_service._reset_process_tree_cache_for_tests()


def test_unread_ended_trees_can_be_marked_read_and_cleared(monkeypatch) -> None:
    process_service._reset_process_tree_cache_for_tests()
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    snapshots = {
        321: ProcessSnapshot(
            pid=321,
            ppid=1,
            pgid=321,
            sid=321,
            state="S",
            started_at=started_at,
            elapsed_seconds=10,
            command_name="codex",
            args=("codex", "exec", "-C", "/work/repo-a"),
            cwd="/work/repo-a",
        ),
        654: ProcessSnapshot(
            pid=654,
            ppid=1,
            pgid=654,
            sid=654,
            state="S",
            started_at=started_at,
            elapsed_seconds=12,
            command_name="codex",
            args=("codex", "exec", "-C", "/work/repo-b"),
            cwd="/work/repo-b",
        ),
    }
    monkeypatch.setattr(process_service, "_scan_processes", lambda now: snapshots)
    assert [tree.pid for tree in ProcessTreeService().list_codex_trees().trees] == [321, 654]

    monkeypatch.setattr(process_service, "_scan_processes", lambda now: {})
    ended = ProcessTreeService().list_codex_trees()
    assert {tree.pid for tree in ended.trees} == {321, 654}
    assert all(tree.lifecycle_status == "ended" and tree.read_at is None for tree in ended.trees)

    response = ProcessTreeService().mark_unread_ended_trees_read_and_clear()

    assert response.cleared_count == 2
    assert ProcessTreeService().list_codex_trees().trees == []
    assert process_service._PROCESS_TREE_CACHE == {}
    process_service._reset_process_tree_cache_for_tests()


def test_read_and_clear_preserves_running_and_read_retained_trees(monkeypatch) -> None:
    process_service._reset_process_tree_cache_for_tests()
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    snapshot = ProcessSnapshot(
        pid=321,
        ppid=1,
        pgid=321,
        sid=321,
        state="S",
        started_at=started_at,
        elapsed_seconds=10,
        command_name="codex",
        args=("codex", "exec", "-C", "/work/repo"),
        cwd="/work/repo",
    )
    monkeypatch.setattr(process_service, "_scan_processes", lambda now: {321: snapshot})
    ProcessTreeService().list_codex_trees()
    response = ProcessTreeService().mark_unread_ended_trees_read_and_clear()
    assert response.cleared_count == 0
    assert 321 in process_service._PROCESS_TREE_CACHE

    monkeypatch.setattr(process_service, "_scan_processes", lambda now: {})
    ProcessTreeService().list_codex_trees()
    assert ProcessTreeService().mark_tree_read(321) is True
    response = ProcessTreeService().mark_unread_ended_trees_read_and_clear()

    assert response.cleared_count == 0
    assert 321 in process_service._PROCESS_TREE_CACHE
    process_service._reset_process_tree_cache_for_tests()


def test_docker_container_codex_is_attached_to_host_launcher(monkeypatch) -> None:
    process_service._reset_process_tree_cache_for_tests()
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    container_id = "a" * 64

    snapshots = {
        10: ProcessSnapshot(
            pid=10,
            ppid=1,
            pgid=10,
            sid=10,
            state="S",
            started_at=started_at,
            elapsed_seconds=60,
            command_name="bash",
            args=("bash", "-lc", "python3 frameworks/assertion/scripts/run_assertion_batch.py"),
            cwd="/work/Schwarz",
        ),
        20: ProcessSnapshot(
            pid=20,
            ppid=10,
            pgid=10,
            sid=10,
            state="S",
            started_at=started_at,
            elapsed_seconds=59,
            command_name="python3",
            args=("python3", "frameworks/assertion/scripts/run_assertion_batch.py", "--jobs", "4"),
            cwd="/work/Schwarz",
        ),
        30: ProcessSnapshot(
            pid=30,
            ppid=20,
            pgid=30,
            sid=30,
            state="S",
            started_at=started_at,
            elapsed_seconds=58,
            command_name="bash",
            args=("bash", "frameworks/assertion/scripts/run_codex_assertion_docker.sh"),
            cwd="/work/Schwarz",
        ),
        100: ProcessSnapshot(
            pid=100,
            ppid=900,
            pgid=100,
            sid=100,
            state="S",
            started_at=started_at,
            elapsed_seconds=57,
            command_name="bash",
            args=("bash", "/usr/local/bin/run_codex_assertion.sh"),
            cwd="/workspace",
            container_id=container_id,
        ),
        110: ProcessSnapshot(
            pid=110,
            ppid=100,
            pgid=100,
            sid=100,
            state="S",
            started_at=started_at,
            elapsed_seconds=56,
            command_name="runuser",
            args=("runuser", "-u", "runner", "--", "codex", "exec", "--cd", "/workspace"),
            cwd="/workspace",
            container_id=container_id,
        ),
        120: ProcessSnapshot(
            pid=120,
            ppid=110,
            pgid=100,
            sid=100,
            state="S",
            started_at=started_at,
            elapsed_seconds=55,
            command_name="node",
            args=("node", "/usr/local/bin/codex", "exec", "--cd", "/workspace"),
            cwd="/workspace",
            container_id=container_id,
        ),
        130: ProcessSnapshot(
            pid=130,
            ppid=120,
            pgid=100,
            sid=100,
            state="S",
            started_at=started_at,
            elapsed_seconds=54,
            command_name="codex",
            args=("codex", "exec", "--cd", "/workspace"),
            cwd="/workspace",
            container_id=container_id,
        ),
    }

    monkeypatch.setattr(process_service, "_scan_processes", lambda now: snapshots)
    monkeypatch.setattr(
        process_service,
        "_docker_lineage_by_container_id",
        lambda snapshots, now: {
            container_id: process_service.DockerContainerLineage(
                container_id=container_id,
                name="schwarz-codex-1779887160-30-18869",
                launcher_pid=30,
            )
        },
    )

    response = ProcessTreeService().list_codex_trees()

    assert [tree.pid for tree in response.trees] == [10]
    root = response.trees[0]
    assert root.role == "Schwarz assertion batch"
    assert root.children[0].pid == 20
    launcher = root.children[0].children[0]
    assert launcher.pid == 30
    assert launcher.role == "Schwarz Docker worker launcher"
    container_root = launcher.children[0]
    assert container_root.pid == 100
    assert container_root.children[0].children[0].children[0].pid == 130

    process_service._reset_process_tree_cache_for_tests()


def test_docker_container_lineage_stops_before_remote_session_boundary(monkeypatch) -> None:
    process_service._reset_process_tree_cache_for_tests()
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    container_id = "d" * 64
    container_name = "schwarz-codex-1779887160-60-18869"

    snapshots = {
        10: ProcessSnapshot(
            pid=10,
            ppid=1,
            pgid=10,
            sid=10,
            state="S",
            started_at=started_at,
            elapsed_seconds=70,
            command_name="sshd",
            args=("sshd", "-D", "[listener]", "0", "of", "10-100", "startups"),
            cwd=None,
        ),
        20: ProcessSnapshot(
            pid=20,
            ppid=10,
            pgid=10,
            sid=10,
            state="S",
            started_at=started_at,
            elapsed_seconds=69,
            command_name="sshd",
            args=("sshd: kejingyu [priv]",),
            cwd=None,
        ),
        30: ProcessSnapshot(
            pid=30,
            ppid=20,
            pgid=10,
            sid=10,
            state="S",
            started_at=started_at,
            elapsed_seconds=68,
            command_name="sshd",
            args=("sshd: kejingyu@notty",),
            cwd=None,
        ),
        40: ProcessSnapshot(
            pid=40,
            ppid=30,
            pgid=40,
            sid=40,
            state="S",
            started_at=started_at,
            elapsed_seconds=67,
            command_name="sh",
            args=("sh",),
            cwd="/home/kejingyu",
        ),
        50: ProcessSnapshot(
            pid=50,
            ppid=40,
            pgid=40,
            sid=40,
            state="S",
            started_at=started_at,
            elapsed_seconds=66,
            command_name="code-f6cfa2ea2403534de03f069bdf160d06451ed282",
            args=(
                "code-f6cfa2ea2403534de03f069bdf160d06451ed282",
                "command-shell",
                "--cli-data-dir",
                "/home/kejingyu/.vscode-server/cli",
            ),
            cwd="/home/kejingyu",
        ),
        60: ProcessSnapshot(
            pid=60,
            ppid=50,
            pgid=60,
            sid=60,
            state="S",
            started_at=started_at,
            elapsed_seconds=65,
            command_name="docker",
            args=("docker", "run", "--rm", "--name", container_name, "schwarz/codex-worker:latest"),
            cwd="/workspace",
        ),
        100: ProcessSnapshot(
            pid=100,
            ppid=900,
            pgid=100,
            sid=100,
            state="S",
            started_at=started_at,
            elapsed_seconds=64,
            command_name="node",
            args=("node", "/usr/local/bin/codex", "exec", "--cd", "/workspace"),
            cwd="/workspace",
            container_id=container_id,
        ),
        110: ProcessSnapshot(
            pid=110,
            ppid=100,
            pgid=100,
            sid=100,
            state="S",
            started_at=started_at,
            elapsed_seconds=63,
            command_name="codex",
            args=("codex", "exec", "--cd", "/workspace"),
            cwd="/workspace",
            container_id=container_id,
        ),
    }

    monkeypatch.setattr(process_service, "_scan_processes", lambda now: snapshots)
    monkeypatch.setattr(
        process_service,
        "_docker_lineage_by_container_id",
        lambda snapshots, now: {
            container_id: process_service.DockerContainerLineage(
                container_id=container_id,
                name=container_name,
                launcher_pid=60,
            )
        },
    )

    response = ProcessTreeService().list_codex_trees()

    assert [tree.pid for tree in response.trees] == [60]
    root = response.trees[0]
    assert root.command_name == "docker"
    assert root.children[0].pid == 100

    process_service._reset_process_tree_cache_for_tests()


def test_docker_container_lineage_stops_before_vscode_extension_host(monkeypatch) -> None:
    process_service._reset_process_tree_cache_for_tests()
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    container_id = "e" * 64

    snapshots = {
        10: ProcessSnapshot(
            pid=10,
            ppid=1,
            pgid=10,
            sid=10,
            state="S",
            started_at=started_at,
            elapsed_seconds=70,
            command_name="node",
            args=(
                "/home/kejingyu/.vscode-server/cli/servers/Stable/server/node",
                "/home/kejingyu/.vscode-server/cli/servers/Stable/server/out/server-main.js",
                "--connection-token=remotessh",
            ),
            cwd="/home/kejingyu",
        ),
        20: ProcessSnapshot(
            pid=20,
            ppid=10,
            pgid=10,
            sid=10,
            state="S",
            started_at=started_at,
            elapsed_seconds=69,
            command_name="node",
            args=(
                "/home/kejingyu/.vscode-server/cli/servers/Stable/server/node",
                "/home/kejingyu/.vscode-server/cli/servers/Stable/server/out/bootstrap-fork",
                "--type=extensionHost",
            ),
            cwd="/home/kejingyu",
        ),
        30: ProcessSnapshot(
            pid=30,
            ppid=20,
            pgid=10,
            sid=10,
            state="S",
            started_at=started_at,
            elapsed_seconds=68,
            command_name="codex",
            args=("/home/kejingyu/.vscode-server/extensions/openai.chatgpt/bin/codex", "app-server"),
            cwd="/home/kejingyu",
        ),
        40: ProcessSnapshot(
            pid=40,
            ppid=30,
            pgid=40,
            sid=40,
            state="S",
            started_at=started_at,
            elapsed_seconds=67,
            command_name="bash",
            args=("bash", "frameworks/assertion/scripts/run_codex_assertion_docker.sh"),
            cwd="/work/Schwarz",
        ),
        100: ProcessSnapshot(
            pid=100,
            ppid=900,
            pgid=100,
            sid=100,
            state="S",
            started_at=started_at,
            elapsed_seconds=66,
            command_name="node",
            args=("node", "/usr/local/bin/codex", "exec", "--cd", "/workspace"),
            cwd="/workspace",
            container_id=container_id,
        ),
        110: ProcessSnapshot(
            pid=110,
            ppid=100,
            pgid=100,
            sid=100,
            state="S",
            started_at=started_at,
            elapsed_seconds=65,
            command_name="codex",
            args=("codex", "exec", "--cd", "/workspace"),
            cwd="/workspace",
            container_id=container_id,
        ),
    }

    monkeypatch.setattr(process_service, "_scan_processes", lambda now: snapshots)
    monkeypatch.setattr(
        process_service,
        "_docker_lineage_by_container_id",
        lambda snapshots, now: {
            container_id: process_service.DockerContainerLineage(
                container_id=container_id,
                name="schwarz-codex-1779887160-40-18869",
                launcher_pid=40,
            )
        },
    )

    response = ProcessTreeService().list_codex_trees()

    assert [tree.pid for tree in response.trees] == [30]
    root = response.trees[0]
    assert root.command_kind == "codex_app_server"
    assert root.children[0].pid == 40
    assert root.children[0].children[0].pid == 100

    process_service._reset_process_tree_cache_for_tests()


def test_docker_container_name_pid_one_is_not_treated_as_launcher(monkeypatch) -> None:
    process_service._reset_process_tree_cache_for_tests()
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    container_id = "b" * 64

    snapshots = {
        1: ProcessSnapshot(
            pid=1,
            ppid=0,
            pgid=1,
            sid=1,
            state="S",
            started_at=started_at,
            elapsed_seconds=60,
            command_name="init",
            args=("/sbin/init",),
            cwd=None,
        ),
        100: ProcessSnapshot(
            pid=100,
            ppid=900,
            pgid=100,
            sid=100,
            state="S",
            started_at=started_at,
            elapsed_seconds=59,
            command_name="node",
            args=("node", "/usr/local/bin/codex", "exec", "--cd", "/workspace"),
            cwd="/workspace",
            container_id=container_id,
        ),
        110: ProcessSnapshot(
            pid=110,
            ppid=100,
            pgid=100,
            sid=100,
            state="S",
            started_at=started_at,
            elapsed_seconds=58,
            command_name="codex",
            args=("codex", "exec", "--cd", "/workspace"),
            cwd="/workspace",
            container_id=container_id,
        ),
    }

    monkeypatch.setattr(process_service, "_scan_processes", lambda now: snapshots)
    monkeypatch.setattr(
        process_service,
        "_docker_lineage_by_container_id",
        lambda snapshots, now: {
            container_id: process_service.DockerContainerLineage(
                container_id=container_id,
                name="cg-omt-tool-prompt-arvo-12312-1-1779973710",
                launcher_pid=1,
            )
        },
    )

    response = ProcessTreeService().list_codex_trees()

    assert [tree.pid for tree in response.trees] == [100]
    assert response.trees[0].children[0].pid == 110

    process_service._reset_process_tree_cache_for_tests()


def test_live_docker_run_process_is_preferred_over_ambiguous_name_pid(monkeypatch) -> None:
    process_service._reset_process_tree_cache_for_tests()
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    container_id = "c" * 64
    container_name = "cg-omt-tool-prompt-arvo-12312-1-1779973710"

    snapshots = {
        1: ProcessSnapshot(
            pid=1,
            ppid=0,
            pgid=1,
            sid=1,
            state="S",
            started_at=started_at,
            elapsed_seconds=60,
            command_name="init",
            args=("/sbin/init",),
            cwd=None,
        ),
        10: ProcessSnapshot(
            pid=10,
            ppid=1,
            pgid=10,
            sid=10,
            state="S",
            started_at=started_at,
            elapsed_seconds=59,
            command_name="bash",
            args=("bash", "-lc", "python3 run_batch.py"),
            cwd="/workflow",
        ),
        20: ProcessSnapshot(
            pid=20,
            ppid=10,
            pgid=10,
            sid=10,
            state="S",
            started_at=started_at,
            elapsed_seconds=58,
            command_name="docker",
            args=("docker", "run", "--rm", "--name", container_name, "schwarz/codex-worker:latest", "codex", "exec"),
            cwd="/workflow",
        ),
        100: ProcessSnapshot(
            pid=100,
            ppid=900,
            pgid=100,
            sid=100,
            state="S",
            started_at=started_at,
            elapsed_seconds=57,
            command_name="node",
            args=("node", "/usr/local/bin/codex", "exec", "--cd", "/workspace"),
            cwd="/workspace",
            container_id=container_id,
        ),
        110: ProcessSnapshot(
            pid=110,
            ppid=100,
            pgid=100,
            sid=100,
            state="S",
            started_at=started_at,
            elapsed_seconds=56,
            command_name="codex",
            args=("codex", "exec", "--cd", "/workspace"),
            cwd="/workspace",
            container_id=container_id,
        ),
    }

    monkeypatch.setattr(process_service, "_scan_processes", lambda now: snapshots)
    monkeypatch.setattr(
        process_service,
        "_docker_lineage_by_container_id",
        lambda snapshots, now: {
            container_id: process_service.DockerContainerLineage(
                container_id=container_id,
                name=container_name,
                launcher_pid=1,
            )
        },
    )

    response = ProcessTreeService().list_codex_trees()

    assert [tree.pid for tree in response.trees] == [10]
    assert response.trees[0].children[0].pid == 20
    assert response.trees[0].children[0].children[0].pid == 100

    process_service._reset_process_tree_cache_for_tests()


def test_codex_descendant_through_non_codex_process_is_not_top_level(monkeypatch) -> None:
    process_service._reset_process_tree_cache_for_tests()
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    snapshots = {
        100: ProcessSnapshot(
            pid=100,
            ppid=1,
            pgid=100,
            sid=100,
            state="S",
            started_at=started_at,
            elapsed_seconds=60,
            command_name="node",
            args=("node", "/home/user/.local/bin/codex", "exec", "--cd", "/workflow"),
            cwd="/workflow",
        ),
        110: ProcessSnapshot(
            pid=110,
            ppid=100,
            pgid=100,
            sid=100,
            state="S",
            started_at=started_at,
            elapsed_seconds=59,
            command_name="codex",
            args=("codex", "exec", "--cd", "/workflow"),
            cwd="/workflow",
        ),
        120: ProcessSnapshot(
            pid=120,
            ppid=110,
            pgid=120,
            sid=100,
            state="S",
            started_at=started_at,
            elapsed_seconds=58,
            command_name="bash",
            args=("bash", "-lc", "for CASE in arvo:1; do python -m cybergym_omt_framework; done"),
            cwd="/workflow",
        ),
        130: ProcessSnapshot(
            pid=130,
            ppid=120,
            pgid=120,
            sid=100,
            state="S",
            started_at=started_at,
            elapsed_seconds=57,
            command_name="python",
            args=("python", "-m", "cybergym_omt_framework", "candidate-loop", "--case", "arvo:1"),
            cwd="/workflow",
        ),
        140: ProcessSnapshot(
            pid=140,
            ppid=130,
            pgid=120,
            sid=100,
            state="S",
            started_at=started_at,
            elapsed_seconds=56,
            command_name="node",
            args=("node", "/home/user/.local/bin/codex", "exec", "--cd", "/workflow/task-workspaces/arvo-1"),
            cwd="/workflow",
        ),
        150: ProcessSnapshot(
            pid=150,
            ppid=140,
            pgid=120,
            sid=100,
            state="S",
            started_at=started_at,
            elapsed_seconds=55,
            command_name="codex",
            args=("codex", "exec", "--cd", "/workflow/task-workspaces/arvo-1"),
            cwd="/workflow/task-workspaces/arvo-1",
        ),
    }

    monkeypatch.setattr(process_service, "_scan_processes", lambda now: snapshots)

    response = ProcessTreeService().list_codex_trees()

    assert [tree.pid for tree in response.trees] == [100]
    root = response.trees[0]
    assert root.children[0].pid == 110
    assert root.children[0].children[0].children[0].children[0].pid == 140
    assert root.children[0].children[0].children[0].children[0].children[0].pid == 150

    process_service._reset_process_tree_cache_for_tests()
