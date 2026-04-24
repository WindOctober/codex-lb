from __future__ import annotations

# ruff: noqa: E501
import asyncio
import json
import os
import signal
import tempfile
import textwrap
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


SCHOLAR_PAPER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "authors": {"type": "string"},
        "venue": {"type": "string"},
        "published_at": {"type": "string"},
        "url": {"type": "string"},
        "source_type": {"type": "string"},
        "summary": {"type": "string"},
        "technical_points": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
        "why_it_matters": {"type": "string"},
    },
    "required": [
        "title",
        "authors",
        "venue",
        "published_at",
        "url",
        "source_type",
        "summary",
        "technical_points",
        "why_it_matters",
    ],
    "additionalProperties": False,
}

PAPER_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "papers": {
            "type": "array",
            "items": SCHOLAR_PAPER_SCHEMA,
            "maxItems": 5,
        }
    },
    "required": ["papers"],
    "additionalProperties": False,
}


class ScholarService:
    def __init__(
        self,
        *,
        project_root: Path,
        cache_file: Path,
        topic_cache_file: Path,
        refresh_interval_seconds: int = 15 * 24 * 60 * 60,
        initial_delay_seconds: int = 7,
    ) -> None:
        self._project_root = project_root
        self._cache_file = cache_file
        self._topic_cache_file = topic_cache_file
        self._refresh_interval = refresh_interval_seconds
        self._initial_delay_seconds = initial_delay_seconds
        self._job_timeout_seconds = int(os.getenv("CODEX_LB_SCHOLAR_JOB_TIMEOUT_SECONDS", "3600"))
        self._max_refresh_seconds = int(
            os.getenv("CODEX_LB_SCHOLAR_MAX_REFRESH_SECONDS", str(self._job_timeout_seconds + 120))
        )
        self._auto_check_seconds = max(
            60,
            min(
                self._refresh_interval,
                int(os.getenv("CODEX_LB_SCHOLAR_AUTO_CHECK_SECONDS", str(15 * 60))),
            ),
        )
        self._max_parallel_jobs = max(1, min(8, int(os.getenv("CODEX_LB_SCHOLAR_MAX_PARALLEL", "8"))))
        self._refresh_lock = asyncio.Lock()
        self._loop_task: asyncio.Task[None] | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._refresh_processes: set[asyncio.subprocess.Process] = set()
        self._snapshot = self._empty_snapshot()

    async def start(self) -> None:
        self._load_cache()
        self._reconcile_loaded_snapshot()
        self._ensure_bootstrap_content()
        self._loop_task = asyncio.create_task(self._run_loop(), name="codex-lb-scholar-loop")

    async def stop(self) -> None:
        tasks = [task for task in (self._loop_task, self._refresh_task) if task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

    def get_snapshot(self) -> dict[str, Any]:
        payload = dict(self._snapshot)
        timed_out = payload.get("refresh_in_progress") and self._is_refresh_overdue(payload.get("last_started_at"))
        if timed_out:
            payload["refresh_in_progress"] = False
            payload["status"] = "ready" if payload.get("topics") else "error"
            payload["background_note"] = "Scholar 后台长刷新超过阈值，当前先展示上一版可用内容。"
        payload["is_stale"] = self._is_stale(payload.get("last_completed_at"))
        payload["next_refresh_due_at"] = self._next_refresh_due_at(payload.get("last_completed_at"))
        return payload

    async def request_refresh(self, *, force: bool = False) -> bool:
        if not force and not self._should_auto_refresh():
            return False
        if self._refresh_task is not None and not self._refresh_task.done():
            if not self._is_refresh_overdue(self._snapshot.get("last_started_at")):
                return False
            await self._cancel_refresh("上一次 Scholar 刷新耗时过长，已停止并准备重试。")
        self._refresh_task = asyncio.create_task(self._refresh(force=force), name="codex-lb-scholar-refresh")
        return True

    async def _run_loop(self) -> None:
        await asyncio.sleep(self._initial_delay_seconds)
        while True:
            await self.request_refresh(force=False)
            await asyncio.sleep(self._auto_check_seconds)

    async def _refresh(self, *, force: bool) -> None:
        async with self._refresh_lock:
            started_at = utcnow().isoformat()
            existing_data = bool(self._snapshot["topics"])
            self._snapshot["status"] = "refreshing"
            self._snapshot["refresh_in_progress"] = True
            self._snapshot["last_started_at"] = started_at
            self._snapshot["last_error"] = None
            self._write_cache()
            try:
                payload = await self._run_codex_refresh()
            except asyncio.CancelledError:
                self._snapshot["refresh_in_progress"] = False
                self._snapshot["status"] = "ready" if existing_data else "error"
                self._write_cache()
                raise
            except Exception as exc:
                self._snapshot["refresh_in_progress"] = False
                self._snapshot["last_error"] = str(exc)
                self._snapshot["status"] = "ready" if existing_data and not force else "error"
                self._write_cache()
                return

            completed_at = utcnow().isoformat()
            self._snapshot.update(
                {
                    "status": "ready",
                    "refresh_in_progress": False,
                    "last_completed_at": completed_at,
                    "last_error": None,
                    "generated_at": payload.get("generated_at", completed_at),
                    "summary": payload.get("summary", ""),
                    "topics": payload.get("topics", []),
                    "disclaimers": payload.get("disclaimers", []),
                }
            )
            self._write_cache()

    async def _run_codex_refresh(self) -> dict[str, Any]:
        api_key = self._load_codex_lb_api_key()
        if not api_key:
            raise RuntimeError("Scholar refresh is not configured with a usable codex-lb API key.")

        topic_config = self._load_topic_cache()
        semaphore = asyncio.Semaphore(self._max_parallel_jobs)

        async def run_job(name: str, prompt: str) -> dict[str, Any]:
            async with semaphore:
                return await self._run_codex_job(api_key=api_key, job_name=name, schema=PAPER_LIST_SCHEMA, prompt=prompt)

        tasks: list[tuple[dict[str, Any], asyncio.Task[dict[str, Any]], asyncio.Task[dict[str, Any]]]] = []
        for topic in topic_config:
            published_task = asyncio.create_task(
                run_job(f"{topic['id']}-published", self._build_published_prompt(topic)),
                name=f"codex-lb-scholar-{topic['id']}-published",
            )
            preprint_task = asyncio.create_task(
                run_job(f"{topic['id']}-preprint", self._build_preprint_prompt(topic)),
                name=f"codex-lb-scholar-{topic['id']}-preprint",
            )
            tasks.append((topic, published_task, preprint_task))

        try:
            topic_payloads = []
            for topic, published_task, preprint_task in tasks:
                published_result, preprint_result = await asyncio.gather(published_task, preprint_task)
                topic_payloads.append(self._merge_topic_payload(topic, published_result, preprint_result))
        except Exception as exc:
            for _, published_task, preprint_task in tasks:
                for task in (published_task, preprint_task):
                    if not task.done():
                        task.cancel()
            await asyncio.gather(
                *[task for _, a, b in tasks for task in (a, b)],
                return_exceptions=True,
            )
            raise RuntimeError(f"Scholar refresh timed out or failed in parallel workers: {exc}") from exc

        return {
            "generated_at": utcnow().isoformat(),
            "summary": self._build_summary(topic_payloads),
            "topics": topic_payloads,
            "disclaimers": [
                "已发表板块按当前 topic cache 里的 CCF-A 会刊提示筛选，不是全量学术数据库导出。",
                "预印本板块故意与已发表板块分开，优先展示仍处在快速传播阶段的新论文。",
                "如果后续增加新的领域，先更新 ccf_topic_cache.json，再触发 Scholar 刷新。",
            ],
        }

    async def _run_codex_job(
        self,
        *,
        api_key: str,
        job_name: str,
        schema: dict[str, Any],
        prompt: str,
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix=f"codex-lb-scholar-{job_name}-") as tmpdir_name:
            tmpdir = Path(tmpdir_name)
            schema_path = tmpdir / "schema.json"
            output_path = tmpdir / "result.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")

            env = os.environ.copy()
            env["OPENAI_API_KEY"] = api_key
            args = [
                "codex",
                "exec",
                "--skip-git-repo-check",
                "-C",
                str(self._project_root),
                "-c",
                'preferred_auth_method="apikey"',
                "-c",
                'model_provider="codex-lb"',
                "-c",
                'model_providers.codex-lb.env_key="OPENAI_API_KEY"',
                "-c",
                f'model_reasoning_effort="{os.getenv("CODEX_LB_SCHOLAR_REASONING", "medium")}"',
                "-m",
                os.getenv("CODEX_LB_SCHOLAR_MODEL", "gpt-5.5"),
                "--output-schema",
                str(schema_path),
                "-o",
                str(output_path),
                prompt,
            ]
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(self._project_root),
                env=env,
                start_new_session=True,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._refresh_processes.add(process)
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self._job_timeout_seconds)
            except asyncio.CancelledError:
                if process.returncode is None:
                    await self._kill_process_group(process)
                raise
            except asyncio.TimeoutError:
                await self._kill_process_group(process)
                raise RuntimeError(f"{job_name} worker timed out while waiting for Codex.") from None
            finally:
                self._refresh_processes.discard(process)

            if process.returncode != 0:
                detail = (stderr or stdout).decode("utf-8", errors="ignore").strip()
                detail = detail[-1200:] if detail else "Codex exited without details."
                raise RuntimeError(f"{job_name} worker failed: {detail}")

            try:
                return json.loads(output_path.read_text(encoding="utf-8"))
            except Exception as exc:
                detail = stdout.decode("utf-8", errors="ignore").strip()[-600:]
                raise RuntimeError(f"{job_name} worker returned invalid JSON. {detail}") from exc

    def _build_published_prompt(self, topic: dict[str, Any]) -> str:
        now = utcnow()
        venue_text = "\n".join(f"- {venue}" for venue in topic.get("ccf_a_venues", []))
        hint_text = "\n".join(f"- {hint}" for hint in topic.get("published_search_hints", []))
        keyword_text = " / ".join(topic.get("keywords", []))
        return textwrap.dedent(
            f"""
            Build the Scholar published-paper board for one topic as strict JSON.

            Current UTC date: {now.date().isoformat()}.
            Current UTC timestamp: {now.isoformat()}.

            Topic label: {topic["label"]}.
            Topic keywords: {keyword_text}.
            Why this topic matters: {topic["why_track"]}.

            You must use the configured playwright MCP at least once during this task.
            Prefer a narrow, repeatable retrieval path such as DBLP result pages, official proceedings pages, journal pages, or arXiv metadata pages used only for confirmation.
            Do not use Python, shell scraping, or ad-hoc command line retrieval. This task must be solved through Codex reasoning plus the configured MCP / browser tools.
            All editorial writing must be in Simplified Chinese.
            Keep titles, venue names, handles, and URLs in their original language.

            This block is only for papers already published in the allowed CCF-A venues below:
            {venue_text}

            Search hints:
            {hint_text}

            Requirements:
            - return up to 5 papers, newest first
            - papers must stay close to the topic rather than only matching one loose keyword
            - only include papers that are already published in one of the allowed venues above
            - source_type should clearly mark them as CCF-A 已发表
            - for each paper, read enough visible paper metadata / abstract / summary text to produce a compact Chinese summary in your own words
            - summary should be 1 to 2 short paragraphs, focused on problem, method, and main result; do not copy the original abstract verbatim
            - technical_points should contain 2 to 4 concise Chinese bullets about method, assumptions, evaluation setting, proof idea, or system design
            - why_it_matters should be 1 to 2 Chinese sentences explaining why this paper matters for the tracked topic
            - authors can be a short comma-separated list

            General rules:
            - do not invent titles, dates, URLs, venues, or authors
            - if the page does not expose enough substance to summarize responsibly, skip that paper instead of fabricating details
            - if fewer than 5 strong matches are available, return fewer rather than guessing
            - return JSON only, matching the provided schema
            """
        ).strip()

    def _build_preprint_prompt(self, topic: dict[str, Any]) -> str:
        now = utcnow()
        hint_text = "\n".join(f"- {hint}" for hint in topic.get("preprint_search_hints", []))
        keyword_text = " / ".join(topic.get("keywords", []))
        return textwrap.dedent(
            f"""
            Build the Scholar preprint board for one topic as strict JSON.

            Current UTC date: {now.date().isoformat()}.
            Current UTC timestamp: {now.isoformat()}.

            Topic label: {topic["label"]}.
            Topic keywords: {keyword_text}.
            Why this topic matters: {topic["why_track"]}.

            You must use the configured playwright MCP at least once during this task.
            Prefer DBLP, arXiv, CoRR, OpenReview, or other public paper landing pages that are visibly preprint-first.
            Do not use Python, shell scraping, or ad-hoc command line retrieval. This task must be solved through Codex reasoning plus the configured MCP / browser tools.
            All editorial writing must be in Simplified Chinese.
            Keep titles, venue names, handles, and URLs in their original language.

            Search hints:
            {hint_text}

            Requirements:
            - return up to 5 papers, newest first
            - this block should favor recent preprints or other not-yet-formally-published papers
            - source_type should clearly mark them as 预印本 or preprint
            - avoid generic surveys unless they are very recent and clearly relevant
            - for each paper, read enough visible paper metadata / abstract / summary text to produce a compact Chinese summary in your own words
            - summary should be 1 to 2 short paragraphs, focused on problem, method, and main result; do not copy the original abstract verbatim
            - technical_points should contain 2 to 4 concise Chinese bullets about method, assumptions, evaluation setting, proof idea, or system design
            - why_it_matters should be 1 to 2 Chinese sentences explaining why this paper matters for the tracked topic
            - authors can be a short comma-separated list

            General rules:
            - do not invent titles, dates, URLs, venues, or authors
            - if the page does not expose enough substance to summarize responsibly, skip that paper instead of fabricating details
            - if fewer than 5 strong matches are available, return fewer rather than guessing
            - return JSON only, matching the provided schema
            """
        ).strip()

    def _merge_topic_payload(
        self,
        topic: dict[str, Any],
        published_result: dict[str, Any],
        preprint_result: dict[str, Any],
    ) -> dict[str, Any]:
        published = list(published_result.get("papers", []))[:5]
        seen_titles = {self._normalize_title(item.get("title", "")) for item in published}
        preprints = []
        for item in preprint_result.get("papers", []):
            title_key = self._normalize_title(item.get("title", ""))
            if not title_key or title_key in seen_titles:
                continue
            preprints.append(item)
            seen_titles.add(title_key)
            if len(preprints) >= 5:
                break
        return {
            "id": topic["id"],
            "label": topic["label"],
            "why_track": topic["why_track"],
            "published": published,
            "preprints": preprints,
        }

    def _load_cache(self) -> None:
        if not self._cache_file.is_file():
            return
        try:
            self._snapshot = json.loads(self._cache_file.read_text(encoding="utf-8"))
        except Exception:
            self._snapshot = self._empty_snapshot()

    def _reconcile_loaded_snapshot(self) -> None:
        if not self._snapshot.get("refresh_in_progress"):
            return
        self._snapshot["refresh_in_progress"] = False
        if self._snapshot.get("last_completed_at"):
            self._snapshot["status"] = "ready"
        else:
            self._snapshot["status"] = "ready" if self._snapshot.get("topics") else "error"
            self._snapshot["last_error"] = self._snapshot.get("last_error") or "上一次 Scholar 刷新在进程重启或中断后未完成。"

    def _ensure_bootstrap_content(self) -> None:
        if self._snapshot.get("topics"):
            return
        self._snapshot.update(self._bootstrap_snapshot())
        self._write_cache()

    def _write_cache(self) -> None:
        self._cache_file.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._cache_file.with_suffix(".tmp")
        temp_path.write_text(json.dumps(self._snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self._cache_file)

    def _load_codex_lb_api_key(self) -> str | None:
        for env_key in ("CODEX_LB_SCHOLAR_API_KEY", "CODEX_LB_API_KEY", "OPENAI_API_KEY"):
            value = os.getenv(env_key)
            if value:
                return value

        auth_toml = Path.home() / ".codex" / "auth.toml"
        if not auth_toml.is_file():
            return None
        try:
            auth = tomllib.loads(auth_toml.read_text(encoding="utf-8"))
        except Exception:
            return None
        value = auth.get("OPENAI_API_KEY")
        return value if isinstance(value, str) and value else None

    def _load_topic_cache(self) -> list[dict[str, Any]]:
        payload = json.loads(self._topic_cache_file.read_text(encoding="utf-8"))
        topics = payload.get("topics", [])
        if not isinstance(topics, list):
            raise RuntimeError("Scholar topic cache is invalid.")
        return [topic for topic in topics if isinstance(topic, dict)]

    async def _cancel_refresh(self, reason: str) -> None:
        for process in list(self._refresh_processes):
            if process.returncode is None:
                await self._kill_process_group(process)
            self._refresh_processes.discard(process)
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        self._snapshot["refresh_in_progress"] = False
        self._snapshot["status"] = "ready" if self._snapshot.get("topics") else "error"
        self._snapshot["last_error"] = reason
        self._write_cache()

    def _empty_snapshot(self) -> dict[str, Any]:
        return {
            "status": "idle",
            "refresh_in_progress": False,
            "last_started_at": None,
            "last_completed_at": None,
            "last_error": None,
            "generated_at": None,
            "summary": "",
            "topics": [],
            "disclaimers": [],
        }

    def _bootstrap_snapshot(self) -> dict[str, Any]:
        topics = self._load_topic_cache()
        return {
            "status": "ready",
            "refresh_in_progress": False,
            "last_started_at": None,
            "last_completed_at": None,
            "last_error": None,
            "generated_at": None,
            "summary": "Scholar 面板已就绪，后台会针对三个领域分别刷新“CCF-A 已发表”和“最新预印本”两块内容。",
            "topics": [
                {
                    "id": topic["id"],
                    "label": topic["label"],
                    "why_track": topic["why_track"],
                    "published": [],
                    "preprints": [],
                }
                for topic in topics
            ],
            "disclaimers": [
                "Scholar 首屏先展示 topic 结构，实际论文列表会在后台 Codex + MCP 刷新完成后填充。",
            ],
        }

    def _normalize_title(self, title: str) -> str:
        return " ".join(title.lower().replace(":", " ").replace("-", " ").split())

    def _is_stale(self, completed_at: str | None) -> bool:
        if not completed_at:
            return True
        try:
            completed = datetime.fromisoformat(completed_at)
        except ValueError:
            return True
        return utcnow() - completed > timedelta(seconds=self._refresh_interval + 30 * 60)

    def _next_refresh_due_at(self, completed_at: str | None) -> str | None:
        if not completed_at:
            return None
        try:
            completed = datetime.fromisoformat(completed_at)
        except ValueError:
            return None
        return (completed + timedelta(seconds=self._refresh_interval)).isoformat()

    def _is_refresh_overdue(self, started_at: str | None) -> bool:
        if not started_at:
            return False
        try:
            started = datetime.fromisoformat(started_at)
        except ValueError:
            return False
        return utcnow() - started > timedelta(seconds=self._max_refresh_seconds)

    def _should_auto_refresh(self) -> bool:
        if self._refresh_task is not None and not self._refresh_task.done():
            return False

        completed_at = self._snapshot.get("last_completed_at")
        if completed_at:
            return self._is_stale(completed_at)

        # Prime the cache once on a fresh install, but do not keep retrying failed
        # refreshes with no successful completion timestamp.
        return not self._snapshot.get("last_started_at") and not self._snapshot.get("generated_at")

    async def _kill_process_group(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()

    def _build_summary(self, topics: list[dict[str, Any]]) -> str:
        parts = []
        for topic in topics:
            published_count = len(topic.get("published", []))
            preprint_count = len(topic.get("preprints", []))
            parts.append(f"{topic['label']} 已发表 {published_count} 篇，预印本 {preprint_count} 篇")
        return "；".join(parts) + "。"


def build_scholar_service() -> ScholarService:
    encryption_key_file = os.getenv("CODEX_LB_ENCRYPTION_KEY_FILE")
    if encryption_key_file:
        project_root = Path(encryption_key_file).expanduser().resolve().parent.parent
    else:
        project_root = Path.cwd().resolve()
    module_dir = Path(__file__).resolve().parent
    cache_file = Path(os.getenv("CODEX_LB_SCHOLAR_CACHE_FILE", project_root / "var" / "scholar-cache.json")).expanduser()
    topic_cache_file = Path(
        os.getenv("CODEX_LB_SCHOLAR_TOPIC_CACHE_FILE", module_dir / "ccf_topic_cache.json")
    ).expanduser()
    refresh_seconds = int(os.getenv("CODEX_LB_SCHOLAR_REFRESH_SECONDS", str(15 * 24 * 60 * 60)))
    initial_delay_seconds = int(os.getenv("CODEX_LB_SCHOLAR_INITIAL_DELAY_SECONDS", "7"))
    return ScholarService(
        project_root=project_root,
        cache_file=cache_file,
        topic_cache_file=topic_cache_file,
        refresh_interval_seconds=refresh_seconds,
        initial_delay_seconds=initial_delay_seconds,
    )
