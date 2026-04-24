from __future__ import annotations

# ruff: noqa: E501
import asyncio
import json
import os
import re
import signal
import tempfile
import textwrap
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


RUMOR_TARGET_COUNT = 9
RUMOR_ITEMS_PER_LANE = 4
RUMOR_BACKFILL_MAX_ATTEMPTS = 2
ITEM_STATE_RETENTION_DAYS = 30


COMPANY_BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "company": {"type": "string"},
        "headline": {"type": "string"},
        "dek": {"type": "string"},
        "theme": {"type": "string"},
        "confidence": {"type": "string"},
        "bullets": {"type": "array", "items": {"type": "string"}},
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "publisher": {"type": "string"},
                    "published_at": {"type": "string"},
                    "source_type": {"type": "string"},
                },
                "required": ["title", "url", "publisher", "published_at", "source_type"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["company", "headline", "dek", "theme", "confidence", "bullets", "sources"],
    "additionalProperties": False,
}

RUMOR_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "summary": {"type": "string"},
        "display_name": {"type": "string"},
        "handle": {"type": "string"},
        "url": {"type": "string"},
        "posted_at": {"type": "string"},
        "engagement_hint": {"type": "string"},
        "why_it_matters": {"type": "string"},
        "verification_status": {"type": "string"},
    },
    "required": [
        "headline",
        "summary",
        "display_name",
        "handle",
        "url",
        "posted_at",
        "engagement_hint",
        "why_it_matters",
        "verification_status",
    ],
    "additionalProperties": False,
}

RUMORS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rumors": {
            "type": "array",
            "items": RUMOR_ITEM_SCHEMA,
            "minItems": RUMOR_ITEMS_PER_LANE,
            "maxItems": RUMOR_ITEMS_PER_LANE,
        }
    },
    "required": ["rumors"],
    "additionalProperties": False,
}

NEWS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "generated_at": {"type": "string"},
        "summary": {"type": "string"},
        "companies": {
            "type": "array",
            "items": {
                **COMPANY_BRIEF_SCHEMA,
            },
        },
        "rumors": {
            "type": "array",
            "items": RUMOR_ITEM_SCHEMA,
        },
        "disclaimers": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["generated_at", "summary", "companies", "rumors", "disclaimers"],
    "additionalProperties": False,
}

NOVELTY_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "current_index": {"type": "integer"},
        "is_new": {"type": "boolean"},
        "matched_previous_index": {"type": ["integer", "null"]},
        "reason": {"type": "string"},
    },
    "required": ["current_index", "is_new", "matched_previous_index", "reason"],
    "additionalProperties": False,
}

NOVELTY_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": NOVELTY_DECISION_SCHEMA,
        }
    },
    "required": ["decisions"],
    "additionalProperties": False,
}


class NewsService:
    def __init__(
        self,
        *,
        project_root: Path,
        cache_file: Path,
        refresh_interval_seconds: int = 6 * 60 * 60,
        initial_delay_seconds: int = 3,
    ) -> None:
        self._project_root = project_root
        self._cache_file = cache_file
        self._refresh_interval = refresh_interval_seconds
        self._initial_delay_seconds = initial_delay_seconds
        self._job_timeout_seconds = int(os.getenv("CODEX_LB_NEWS_JOB_TIMEOUT_SECONDS", "3600"))
        self._max_refresh_seconds = int(
            os.getenv("CODEX_LB_NEWS_MAX_REFRESH_SECONDS", str(self._job_timeout_seconds + 120))
        )
        self._auto_check_seconds = max(
            60,
            min(
                self._refresh_interval,
                int(os.getenv("CODEX_LB_NEWS_AUTO_CHECK_SECONDS", str(10 * 60))),
            ),
        )
        self._max_parallel_jobs = max(1, min(8, int(os.getenv("CODEX_LB_NEWS_MAX_PARALLEL", "8"))))
        self._refresh_lock = asyncio.Lock()
        self._loop_task: asyncio.Task[None] | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._refresh_processes: set[asyncio.subprocess.Process] = set()
        self._snapshot = self._empty_snapshot()

    async def start(self) -> None:
        self._load_cache()
        self._reconcile_loaded_snapshot()
        self._ensure_bootstrap_content()
        self._ensure_item_state(seed_existing_items=True)
        self._loop_task = asyncio.create_task(self._run_loop(), name="codex-lb-news-loop")

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
        payload = json.loads(json.dumps(self._snapshot))
        timed_out = payload.get("refresh_in_progress") and self._is_refresh_overdue(payload.get("last_started_at"))
        if timed_out:
            payload["refresh_in_progress"] = False
            payload["status"] = "ready" if payload.get("companies") or payload.get("rumors") else "error"
            payload["background_note"] = "后台长刷新超过时长阈值，当前先展示上一版可用内容。"
        payload["is_stale"] = self._is_stale(payload.get("last_completed_at"))
        payload["next_refresh_due_at"] = self._next_refresh_due_at(payload.get("last_completed_at"))
        payload["companies"] = self._sort_companies(payload.get("companies", []))
        payload["rumors"] = self._sort_rumors(payload.get("rumors", []))
        payload.pop("item_state", None)
        return payload

    async def request_refresh(self, *, force: bool = False) -> bool:
        if not force and not self._should_auto_refresh():
            return False
        if self._refresh_task is not None and not self._refresh_task.done():
            if not self._is_refresh_overdue(self._snapshot.get("last_started_at")):
                return False
            await self._cancel_refresh("上一次后台刷新耗时过长，已停止并准备重试。")
        self._refresh_task = asyncio.create_task(self._refresh(force=force), name="codex-lb-news-refresh")
        return True

    async def _run_loop(self) -> None:
        await asyncio.sleep(self._initial_delay_seconds)
        while True:
            await self.request_refresh(force=False)
            await asyncio.sleep(self._auto_check_seconds)

    async def _refresh(self, *, force: bool) -> None:
        async with self._refresh_lock:
            started_at = utcnow().isoformat()
            existing_data = bool(self._snapshot["companies"] or self._snapshot["rumors"])
            previous_snapshot = json.loads(json.dumps(self._snapshot))
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
            previous_companies = self._sort_companies(previous_snapshot.get("companies", []))
            previous_rumors = self._sort_rumors(previous_snapshot.get("rumors", []))
            current_companies = self._sort_companies(payload.get("companies", []))
            current_rumors = self._sort_rumors(payload.get("rumors", []))
            company_new_flags, rumor_new_flags = await asyncio.gather(
                self._classify_semantic_novelty(
                    job_name="novelty-companies",
                    section="companies",
                    previous=previous_companies,
                    current=current_companies,
                ),
                self._classify_semantic_novelty(
                    job_name="novelty-rumors",
                    section="rumors",
                    previous=previous_rumors,
                    current=current_rumors,
                ),
            )
            companies = self._mark_company_novelty(current_companies, completed_at, company_new_flags)
            rumors = self._mark_rumor_novelty(current_rumors, completed_at, rumor_new_flags)
            self._snapshot.update(
                {
                    "status": "ready",
                    "refresh_in_progress": False,
                    "last_completed_at": completed_at,
                    "last_error": None,
                    "generated_at": payload.get("generated_at", completed_at),
                    "summary": payload.get("summary", ""),
                    "companies": companies,
                    "rumors": rumors,
                    "disclaimers": payload.get("disclaimers", []),
                }
            )
            self._prune_item_state(reference_time=completed_at)
            self._write_cache()

    async def _run_codex_refresh(self) -> dict[str, Any]:
        api_key = self._load_codex_lb_api_key()
        if not api_key:
            raise RuntimeError("News refresh is not configured with a usable codex-lb API key.")

        semaphore = asyncio.Semaphore(self._max_parallel_jobs)

        async def run_job(name: str, schema: dict[str, Any], prompt: str) -> dict[str, Any]:
            async with semaphore:
                return await self._run_codex_job(api_key=api_key, job_name=name, schema=schema, prompt=prompt)

        openai_task = asyncio.create_task(
            run_job("openai", COMPANY_BRIEF_SCHEMA, self._build_company_prompt("OpenAI")),
            name="codex-lb-news-openai",
        )
        anthropic_task = asyncio.create_task(
            run_job("anthropic", COMPANY_BRIEF_SCHEMA, self._build_company_prompt("Anthropic")),
            name="codex-lb-news-anthropic",
        )
        rumors_openai_task = asyncio.create_task(
            run_job("rumors-openai", RUMORS_OUTPUT_SCHEMA, self._build_rumors_prompt("OpenAI / Codex / GPT line")),
            name="codex-lb-news-rumors-openai",
        )
        rumors_anthropic_task = asyncio.create_task(
            run_job("rumors-anthropic", RUMORS_OUTPUT_SCHEMA, self._build_rumors_prompt("Anthropic / Claude line")),
            name="codex-lb-news-rumors-anthropic",
        )
        rumors_general_task = asyncio.create_task(
            run_job("rumors-general", RUMORS_OUTPUT_SCHEMA, self._build_rumors_prompt("Broader AI rumor line")),
            name="codex-lb-news-rumors-general",
        )

        try:
            openai_payload, anthropic_payload, rumors_openai_payload, rumors_anthropic_payload, rumors_general_payload = await asyncio.gather(
                openai_task,
                anthropic_task,
                rumors_openai_task,
                rumors_anthropic_task,
                rumors_general_task,
            )
        except Exception as exc:
            for task in (
                openai_task,
                anthropic_task,
                rumors_openai_task,
                rumors_anthropic_task,
                rumors_general_task,
            ):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                openai_task,
                anthropic_task,
                rumors_openai_task,
                rumors_anthropic_task,
                rumors_general_task,
                return_exceptions=True,
            )
            raise RuntimeError(f"News refresh timed out or failed in parallel workers: {exc}") from exc

        companies = [openai_payload, anthropic_payload]
        rumors = self._merge_rumors(
            rumors_openai_payload.get("rumors", []),
            rumors_anthropic_payload.get("rumors", []),
            rumors_general_payload.get("rumors", []),
        )
        for attempt in range(1, RUMOR_BACKFILL_MAX_ATTEMPTS + 1):
            if len(rumors) >= RUMOR_TARGET_COUNT:
                break
            backfill_payload = await run_job(
                f"rumors-backfill-{attempt}",
                RUMORS_OUTPUT_SCHEMA,
                self._build_rumors_prompt(
                    "AI ecosystem / labs / tooling rumor backfill line",
                    exclude_items=rumors,
                    min_unique_needed=RUMOR_TARGET_COUNT - len(rumors),
                ),
            )
            rumors = self._merge_rumors(rumors, backfill_payload.get("rumors", []))
        generated_at = utcnow().isoformat()
        return {
            "generated_at": generated_at,
            "summary": self._build_summary(companies, rumors),
            "companies": companies,
            "rumors": rumors,
            "disclaimers": [
                "未证实板块故意保留为高热度 X 信源，不代表事实已确认。",
                "当前这版内容由 5 条并发的 Codex + MCP 刷新任务汇总生成，默认并发上限为 8。",
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
        with tempfile.TemporaryDirectory(prefix=f"codex-lb-news-{job_name}-") as tmpdir_name:
            tmpdir = Path(tmpdir_name)
            schema_path = tmpdir / "schema.json"
            output_path = tmpdir / "news.json"
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
                f'model_reasoning_effort="{os.getenv("CODEX_LB_NEWS_REASONING", "medium")}"',
                "-m",
                os.getenv("CODEX_LB_NEWS_MODEL", "gpt-5.5"),
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
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self._job_timeout_seconds,
                )
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

    def _build_company_prompt(self, company: str) -> str:
        now = utcnow()
        company_spec = {
            "OpenAI": {
                "label": "OpenAI",
                "site": "openai.com",
                "account": "@OpenAI",
                "other_handles": "@sama @gdb",
                "theme": "OpenAI 近 7 天最重要的已确认动态",
            },
            "Anthropic": {
                "label": "Anthropic",
                "site": "anthropic.com",
                "account": "@claudeai",
                "other_handles": "@AnthropicAI",
                "theme": "Anthropic / Claude 近 7 天最重要的已确认动态",
            },
        }[company]
        return textwrap.dedent(
            f"""
            Build one confirmed company briefing as strict JSON.

            Current UTC date: {now.date().isoformat()}.
            Current UTC timestamp: {now.isoformat()}.

            Company focus: {company_spec["theme"]}.
            Primary website: {company_spec["site"]}.
            Primary X account: {company_spec["account"]}.
            Additional handles if useful: {company_spec["other_handles"]}.

            You must use the configured playwright MCP against x.com at least once during this task.
            You should use the same playwright MCP on the official website only if needed.
            Keep this task fast and selective. Do not perform exhaustive research.
            All editorial writing must be in Simplified Chinese.
            Keep source titles, product names, company handles, and URLs in their original language when needed.

            Requirements:
            - return exactly one company briefing object
            - use a narrow source set: official newsroom / blog pages plus official X accounts when enough
            - prioritize the single most important very recent development, ideally within the last 7 days
            - prefer official company sources or direct executive / official X posts
            - keep the headline sharp and the dek concise
            - bullets should be short factual takeaways, maximum 3 bullets
            - sources should be limited to 1 or 2 high-signal items and must include direct URLs and a published_at string
            - confidence should clearly signal confirmed / official status

            General rules:
            - do not invent dates, URLs, publishers, or engagement
            - use concise product-news language in Chinese
            - if a field is unknown, use an empty string instead of guessing
            - return JSON only, matching the provided schema
            """
        ).strip()

    def _build_rumors_prompt(
        self,
        lane: str,
        *,
        exclude_items: list[dict[str, Any]] | None = None,
        min_unique_needed: int | None = None,
    ) -> str:
        now = utcnow()
        exclusion_block = ""
        if exclude_items:
            exclusion_lines = []
            for item in exclude_items:
                if not isinstance(item, dict):
                    continue
                headline = str(item.get("headline", "")).strip()
                url = str(item.get("url", "")).strip()
                if headline or url:
                    exclusion_lines.append(f"- {headline} | {url}")
            if exclusion_lines:
                exclusion_block = (
                    "\n\nAlready selected rumors to avoid repeating in this run:\n"
                    + "\n".join(exclusion_lines[:24])
                )
        unique_target_line = ""
        if min_unique_needed is not None:
            unique_target_line = (
                f"\n- at least {max(1, min_unique_needed)} of the returned posts must be new unique additions beyond the exclusion list"
            )
        return textwrap.dedent(
            f"""
            Build the X rumor board as strict JSON.

            Current UTC date: {now.date().isoformat()}.
            Current UTC timestamp: {now.isoformat()}.

            You must use the configured playwright MCP against x.com during this task.
            Keep this task fast and selective.
            All editorial writing must be in Simplified Chinese.
            Keep source titles, product names, company handles, and URLs in their original language when needed.

            Search lane: {lane}.
            Produce exactly {RUMOR_ITEMS_PER_LANE} rumor posts.
            {exclusion_block}

            Requirements:
            - source them from X using the playwright MCP
            - all {RUMOR_ITEMS_PER_LANE} must be AI-related and from within the last 72 hours
            - stay mostly within the designated search lane above
            - use only a small number of high-signal searches such as OpenAI, Anthropic, Claude, GPT, Gemini, Grok, Codex, or AI
            - prefer posts with visible engagement or obvious traction
            - do not include official announcements in this section
            - verification_status must make it clear they are unconfirmed or second-hand
            - engagement_hint should be a short text summary, not invented exact numbers unless visible
            - avoid returning the same X post or same screenshot cluster twice within this lane
            - choose diverse topics rather than repeating the same rumor
            {unique_target_line}

            General rules:
            - do not invent dates, URLs, publishers, or engagement
            - use concise product-news language in Chinese
            - if a field is unknown, use an empty string instead of guessing
            - return JSON only, matching the provided schema
            """
        ).strip()

    async def _classify_semantic_novelty(
        self,
        *,
        job_name: str,
        section: str,
        previous: list[dict[str, Any]],
        current: list[dict[str, Any]],
    ) -> list[bool]:
        if not current:
            return []

        api_key = self._load_codex_lb_api_key()
        if not api_key:
            return self._fallback_novelty_flags(section=section, previous=previous, current=current)

        try:
            payload = await self._run_codex_job(
                api_key=api_key,
                job_name=job_name,
                schema=NOVELTY_OUTPUT_SCHEMA,
                prompt=self._build_novelty_prompt(section=section, previous=previous, current=current),
            )
        except Exception:
            return self._fallback_novelty_flags(section=section, previous=previous, current=current)

        fallback_flags = self._fallback_novelty_flags(section=section, previous=previous, current=current)
        resolved = {index + 1: flag for index, flag in enumerate(fallback_flags)}
        for decision in payload.get("decisions", []):
            if not isinstance(decision, dict):
                continue
            current_index = decision.get("current_index")
            if not isinstance(current_index, int) or current_index < 1 or current_index > len(current):
                continue
            is_new = decision.get("is_new")
            if isinstance(is_new, bool):
                resolved[current_index] = is_new
        return [bool(resolved.get(index + 1, fallback_flags[index])) for index in range(len(current))]

    def _build_novelty_prompt(
        self,
        *,
        section: str,
        previous: list[dict[str, Any]],
        current: list[dict[str, Any]],
    ) -> str:
        section_label = "已确认动态" if section == "companies" else "未证实传闻"
        section_rules = {
            "companies": (
                "- 同一家公司、同一轮官方动态，即使 headline/dek 改写、来源 URL 换成另一条官方帖文，也算旧消息\n"
                "- 只有当核心事件明显不同，例如新的产品发布、能力上线、合作公告、价格或政策变化，才算新消息"
            ),
            "rumors": (
                "- 同一 rumor/claim 被不同账号转述、截图复述、二次总结，哪怕 URL 完全不同，也算旧消息\n"
                "- 只有当核心 claim 明显不同，或原 rumor 出现了实质性新进展，才算新消息"
            ),
        }[section]
        previous_payload = self._prepare_novelty_items(section=section, items=previous)
        current_payload = self._prepare_novelty_items(section=section, items=current)
        return textwrap.dedent(
            f"""
            Decide which current {section_label} items are genuinely new compared with the previous snapshot.

            Use only the provided lists. Do not browse, do not use MCP, and do not invent missing facts.
            Compare by semantic consistency of the underlying story, not by URL equality.
            If a current item is the same story as any previous item, mark it as not new even if the wording or source URL changed.

            Section-specific rules:
            {section_rules}

            Previous snapshot items:
            {json.dumps(previous_payload, ensure_ascii=False, indent=2)}

            Current snapshot items:
            {json.dumps(current_payload, ensure_ascii=False, indent=2)}

            Output requirements:
            - return one decision per current item
            - current_index is 1-based and refers to the current snapshot list
            - matched_previous_index is the matched previous item index when not new, otherwise null
            - reason should be short Simplified Chinese
            - JSON only
            """
        ).strip()

    def _prepare_novelty_items(self, *, section: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            base = {
                "index": index,
                "headline": str(item.get("headline", "")).strip(),
            }
            if section == "companies":
                base.update(
                    {
                        "company": str(item.get("company", "")).strip(),
                        "theme": str(item.get("theme", "")).strip(),
                        "dek": str(item.get("dek", "")).strip(),
                        "sources": [
                            {
                                "title": str(source.get("title", "")).strip(),
                                "publisher": str(source.get("publisher", "")).strip(),
                                "published_at": str(source.get("published_at", "")).strip(),
                            }
                            for source in item.get("sources", [])
                            if isinstance(source, dict)
                        ],
                    }
                )
            else:
                base.update(
                    {
                        "posted_at": str(item.get("posted_at", "")).strip(),
                        "display_name": str(item.get("display_name", "")).strip(),
                        "handle": str(item.get("handle", "")).strip(),
                        "summary": str(item.get("summary", "")).strip(),
                        "why_it_matters": str(item.get("why_it_matters", "")).strip(),
                    }
                )
            prepared.append(base)
        return prepared

    def _load_cache(self) -> None:
        if not self._cache_file.is_file():
            return
        try:
            self._snapshot = json.loads(self._cache_file.read_text(encoding="utf-8"))
        except Exception:
            self._snapshot = self._empty_snapshot()

    def _reconcile_loaded_snapshot(self) -> None:
        if not self._snapshot.get("refresh_in_progress"):
            self._normalize_item_state()
            return
        self._snapshot["refresh_in_progress"] = False
        if self._snapshot.get("last_completed_at"):
            self._snapshot["status"] = "ready"
        else:
            self._snapshot["status"] = "ready" if self._snapshot.get("companies") or self._snapshot.get("rumors") else "error"
            self._snapshot["last_error"] = self._snapshot.get("last_error") or "上一次 News 刷新在进程重启或中断后未完成。"
        self._normalize_item_state()

    def _ensure_bootstrap_content(self) -> None:
        if (self._snapshot.get("companies") or self._snapshot.get("rumors")) and not self._looks_like_legacy_english_bootstrap():
            return
        self._snapshot.update(self._bootstrap_snapshot())
        self._write_cache()

    def _write_cache(self) -> None:
        self._cache_file.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._cache_file.with_suffix(".tmp")
        temp_path.write_text(json.dumps(self._snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self._cache_file)

    def _load_codex_lb_api_key(self) -> str | None:
        for env_key in ("CODEX_LB_NEWS_API_KEY", "CODEX_LB_API_KEY", "OPENAI_API_KEY"):
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
        self._snapshot["status"] = "ready" if self._snapshot.get("companies") or self._snapshot.get("rumors") else "error"
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
            "companies": [],
            "rumors": [],
            "disclaimers": [],
            "item_state": {
                "seen_at": {},
                "read_at": {},
            },
        }

    def _bootstrap_snapshot(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "refresh_in_progress": False,
            "last_started_at": None,
            "last_completed_at": "2026-04-22T11:05:00+00:00",
            "last_error": None,
            "generated_at": "2026-04-22T11:05:00+00:00",
            "summary": (
                "当前最明确的两条主线是：OpenAI 正在集中推进 ChatGPT Images 2.0，Anthropic 则把重心放在算力扩张和 Claude 新功能上；与此同时，X 上最热的未证实讨论集中在 Codex 模型名泄露、Claude Mythos 传闻，以及 OpenAI 新网络安全产品的消息。"
            ),
            "companies": [
                {
                    "company": "OpenAI",
                    "headline": "ChatGPT Images 2.0 已经成为 OpenAI 这轮对外叙事的核心",
                    "dek": (
                        "OpenAI 正在用产品页、直播和官方 X 账号同步推进一轮图像生成升级，核心卖点是更强的编辑能力、更稳定的版式控制，以及更接近“先思考再生成”的工作流。"
                    ),
                    "theme": "产品发布",
                    "confidence": "官方确认",
                    "bullets": [
                        "OpenAI 已在 2026 年 4 月 21 日正式发布 ChatGPT Images 2.0。",
                        "官方 X 账号当前的置顶和主要传播内容，都围绕这次图像模型升级展开。",
                        "后续线程进一步把它定义为具备更强思考与编辑能力的新一代图像模型。",
                    ],
                    "sources": [
                        {
                            "title": "Introducing ChatGPT Images 2.0",
                            "url": "https://openai.com/index/introducing-chatgpt-images-2-0/",
                            "publisher": "OpenAI",
                            "published_at": "2026-04-21",
                            "source_type": "official website",
                        },
                        {
                            "title": "OpenAI on X: Introducing ChatGPT Images 2.0",
                            "url": "https://x.com/OpenAI/status/2046670977145372771",
                            "publisher": "OpenAI",
                            "published_at": "2026-04-21",
                            "source_type": "official X account",
                        },
                    ],
                },
                {
                    "company": "Anthropic",
                    "headline": "Anthropic 这轮节奏同时押注算力扩张和 Claude 的可用性升级",
                    "dek": (
                        "Anthropic 当前最确定的主线不是单点模型发布，而是更大的基础设施投入，加上 Claude 在实际工作流里的功能增强，例如可持续刷新的 live artifacts。"
                    ),
                    "theme": "算力与产品并进",
                    "confidence": "官方确认",
                    "bullets": [
                        "Anthropic 在 2026 年 4 月 20 日宣布与 Amazon 扩大合作，目标是拿到最高 5 吉瓦算力。",
                        "公司在公告里明确提到，Claude 的企业和消费端需求增长已经把可用性推到更高优先级。",
                        "Claude 官方 X 最近重点宣传的是 Cowork 里的 live artifacts，可自动刷新仪表盘和追踪器。",
                    ],
                    "sources": [
                        {
                            "title": "Anthropic and Amazon expand collaboration for up to 5 gigawatts of new compute",
                            "url": "https://www.anthropic.com/news/anthropic-amazon-compute",
                            "publisher": "Anthropic",
                            "published_at": "2026-04-20",
                            "source_type": "official website",
                        },
                        {
                            "title": "Claude on X: live artifacts in Cowork",
                            "url": "https://x.com/claudeai/status/2046328619249684989",
                            "publisher": "Claude",
                            "published_at": "2026-04-21",
                            "source_type": "official X account",
                        },
                    ],
                },
            ],
            "rumors": [
                {
                    "headline": "X 上热传 Codex 选择器里出现 GPT-5.5 等内部模型名",
                    "summary": "多位用户和资讯账号正在传播截图，称 Codex 的模型选择器里短暂出现了尚未正式发布的 OpenAI 内部模型名称，其中 GPT-5.5 最受关注。",
                    "display_name": "TestingCatalog News",
                    "handle": "@testingcatalog",
                    "url": "https://x.com/testingcatalog/status/2046892580449693711",
                    "posted_at": "2026-04-22",
                    "engagement_hint": "抓取时约为 180 likes、18K views。",
                    "why_it_matters": "如果截图属实，说明 OpenAI 的下一轮模型发布可能已经进入临近上线的内部准备阶段。",
                    "verification_status": "X 高热讨论，未证实",
                },
                {
                    "headline": "X 上有人称 Claude Mythos 曾在 Discord 提前泄露",
                    "summary": "一条高互动帖子声称，名为 Claude Mythos 的模型曾短暂暴露给部分用户，而且它可能是比当前公开版本更受限、也更敏感的一支 Anthropic 模型。",
                    "display_name": "Chubby",
                    "handle": "@kimmonismus",
                    "url": "https://x.com/kimmonismus/status/2046874230529180092",
                    "posted_at": "2026-04-22",
                    "engagement_hint": "抓取时约为 842 likes、47K views。",
                    "why_it_matters": "这会强化外界对 Anthropic 正在测试更高阶、但尚未公开的模型分支的猜测。",
                    "verification_status": "X 高热讨论，未证实",
                },
                {
                    "headline": "二手信源称 OpenAI 已向盟友简报新的网络安全产品",
                    "summary": "一条传播较广的 X 帖子援引 Axios 称，OpenAI 已向美国政府和 Five Eyes 盟友介绍一款新的网络安全产品。",
                    "display_name": "Open Source Intel",
                    "handle": "@Osint613",
                    "url": "https://x.com/Osint613/status/2046878506257207331",
                    "posted_at": "2026-04-22",
                    "engagement_hint": "抓取时约为 175 likes、22K views。",
                    "why_it_matters": "如果属实，这意味着 OpenAI 可能正在把前沿模型能力更明确地封装进网络安全或防务场景。",
                    "verification_status": "二手信源，未在此处独立证实",
                },
            ],
            "disclaimers": [
                "未证实板块故意保留为高热度 X 信源，不代表事实已确认。",
                "当前这版内容会先立即展示，后台仍会继续运行更完整的 Codex + MCP 刷新任务。",
            ],
        }

    def _looks_like_legacy_english_bootstrap(self) -> bool:
        summary = self._snapshot.get("summary")
        if isinstance(summary, str) and summary.startswith("OpenAI's image push and Anthropic's compute expansion"):
            return True
        rumors = self._snapshot.get("rumors")
        if isinstance(rumors, list) and rumors:
            headline = rumors[0].get("headline") if isinstance(rumors[0], dict) else None
            if isinstance(headline, str) and headline.startswith("Codex users spotted GPT-5.5"):
                return True
        return False

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
        return not self._snapshot.get("last_started_at") and not self._snapshot.get("generated_at")

    async def _kill_process_group(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()

    def mark_all_read(self) -> int:
        self._normalize_item_state()
        read_at = self._snapshot["item_state"]["read_at"]
        seen_at = self._snapshot["item_state"]["seen_at"]
        timestamp = utcnow().isoformat()
        marked = 0

        for collection_name, identity_builder in (
            ("companies", self._company_identity),
            ("rumors", self._rumor_identity),
        ):
            for item in self._snapshot.get(collection_name, []):
                if not isinstance(item, dict):
                    continue
                identity = identity_builder(item)
                if not identity:
                    continue
                if identity not in read_at:
                    marked += 1
                seen_at.setdefault(identity, timestamp)
                read_at[identity] = timestamp
                item["is_new"] = False

        self._write_cache()
        return marked

    def _mark_company_novelty(
        self,
        current: list[dict[str, Any]],
        seen_at_time: str,
        novelty_flags: list[bool] | None = None,
    ) -> list[dict[str, Any]]:
        self._normalize_item_state()
        seen_at = self._snapshot["item_state"]["seen_at"]
        read_at = self._snapshot["item_state"]["read_at"]
        marked: list[dict[str, Any]] = []
        for index, item in enumerate(current):
            payload = dict(item)
            identity = self._company_identity(payload)
            if identity:
                seen_at.setdefault(identity, seen_at_time)
            ai_is_new = novelty_flags[index] if novelty_flags is not None and index < len(novelty_flags) else True
            payload["is_new"] = bool(identity) and bool(ai_is_new) and identity not in read_at
            marked.append(payload)
        return marked

    def _mark_rumor_novelty(
        self,
        current: list[dict[str, Any]],
        seen_at_time: str,
        novelty_flags: list[bool] | None = None,
    ) -> list[dict[str, Any]]:
        self._normalize_item_state()
        seen_at = self._snapshot["item_state"]["seen_at"]
        read_at = self._snapshot["item_state"]["read_at"]
        marked: list[dict[str, Any]] = []
        for index, item in enumerate(current):
            payload = dict(item)
            identity = self._rumor_identity(payload)
            if identity:
                seen_at.setdefault(identity, seen_at_time)
            ai_is_new = novelty_flags[index] if novelty_flags is not None and index < len(novelty_flags) else True
            payload["is_new"] = bool(identity) and bool(ai_is_new) and identity not in read_at
            marked.append(payload)
        return marked

    def _fallback_novelty_flags(
        self,
        *,
        section: str,
        previous: list[dict[str, Any]],
        current: list[dict[str, Any]],
    ) -> list[bool]:
        if section == "companies":
            previous_signatures = {self._company_semantic_signature(item) for item in previous if isinstance(item, dict)}
            return [self._company_semantic_signature(item) not in previous_signatures for item in current]
        previous_signatures = {self._rumor_semantic_signature(item) for item in previous if isinstance(item, dict)}
        return [self._rumor_semantic_signature(item) not in previous_signatures for item in current]

    def _company_semantic_signature(self, item: dict[str, Any]) -> str:
        source_titles = " ".join(
            self._normalize_text(str(source.get("title", "")))
            for source in item.get("sources", [])
            if isinstance(source, dict)
        )
        return "::".join(
            [
                self._normalize_text(str(item.get("company", ""))),
                self._normalize_text(str(item.get("headline", ""))),
                self._normalize_text(str(item.get("theme", ""))),
                source_titles,
            ]
        )

    def _rumor_semantic_signature(self, item: dict[str, Any]) -> str:
        return "::".join(
            [
                self._normalize_text(str(item.get("headline", ""))),
                self._normalize_text(str(item.get("summary", ""))),
                self._normalize_text(str(item.get("why_it_matters", ""))),
            ]
        )

    def _company_identity(self, item: dict[str, Any]) -> str:
        source_urls = "|".join(
            sorted(
                self._normalize_url(str(source.get("url", "")).strip())
                for source in item.get("sources", [])
                if isinstance(source, dict) and str(source.get("url", "")).strip()
            )
        )
        return "::".join(
            [
                str(item.get("company", "")).strip().lower(),
                source_urls,
            ]
        )

    def _rumor_identity(self, item: dict[str, Any]) -> str:
        url = self._normalize_url(str(item.get("url", "")).strip())
        if url:
            return url
        return "::".join(
            [
                str(item.get("headline", "")).strip().lower(),
                str(item.get("handle", "")).strip().lower(),
                str(item.get("posted_at", "")).strip().lower(),
            ]
        )

    def _build_summary(self, companies: list[dict[str, Any]], rumors: list[dict[str, Any]]) -> str:
        company_lines = [item.get("headline", "").strip() for item in companies if item.get("headline")]
        rumor_lines = [item.get("headline", "").strip() for item in rumors if item.get("headline")]
        company_text = "，".join(company_lines[:2]) if company_lines else "官方主线仍在整理中"
        rumor_text = "、".join(rumor_lines[:4]) if rumor_lines else "暂无进入展示区的高热未证实讨论"
        return f"当前最明确的已确认主线是：{company_text}；未证实高热讨论主要集中在 {rumor_text}。"

    def _merge_rumors(self, *groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for group in groups:
            for item in group:
                if not isinstance(item, dict):
                    continue
                identity = self._rumor_identity(item)
                if identity and identity in seen_ids:
                    continue
                if identity:
                    seen_ids.add(identity)
                merged.append(item)
        return self._sort_rumors(merged)[:RUMOR_TARGET_COUNT]

    def _sort_companies(self, companies: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in companies:
            if not isinstance(item, dict):
                continue
            payload = dict(item)
            payload["sources"] = self._sort_sources(payload.get("sources", []))
            normalized.append(payload)
        return sorted(normalized, key=self._company_sort_key, reverse=True)

    def _sort_rumors(self, rumors: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized = [dict(item) for item in rumors if isinstance(item, dict)]
        return sorted(normalized, key=lambda item: self._sort_timestamp(item.get("posted_at")), reverse=True)

    def _sort_sources(self, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized = [dict(source) for source in sources if isinstance(source, dict)]
        return sorted(normalized, key=lambda source: self._sort_timestamp(source.get("published_at")), reverse=True)

    def _company_sort_key(self, item: dict[str, Any]) -> datetime:
        sources = item.get("sources", [])
        if isinstance(sources, list):
            for source in sources:
                if not isinstance(source, dict):
                    continue
                parsed = self._parse_datetime(source.get("published_at"))
                if parsed is not None:
                    return parsed
        return self._sort_timestamp(item.get("updated_at"))

    def _sort_timestamp(self, value: Any) -> datetime:
        parsed = self._parse_datetime(value)
        return parsed if parsed is not None else datetime.min.replace(tzinfo=timezone.utc)

    def _parse_datetime(self, value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if cleaned.endswith("Z"):
            cleaned = f"{cleaned[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(cleaned)
        except ValueError:
            for pattern in ("%Y-%m-%d", "%Y/%m/%d"):
                try:
                    parsed = datetime.strptime(cleaned, pattern)
                    break
                except ValueError:
                    continue
            else:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _normalize_url(self, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            return ""
        return normalized.split("?", 1)[0].rstrip("/").lower()

    def _normalize_text(self, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            return ""
        normalized = re.sub(r"https?://\S+", " ", normalized)
        normalized = re.sub(r"[@#`*_~\[\](){}|:;,.!?/\\\\\"'+=-]+", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized)
        return normalized.strip()

    def _normalize_item_state(self) -> None:
        state = self._snapshot.get("item_state")
        if not isinstance(state, dict):
            state = {}
        seen_at = state.get("seen_at")
        read_at = state.get("read_at")
        self._snapshot["item_state"] = {
            "seen_at": {
                str(key): str(value)
                for key, value in (seen_at.items() if isinstance(seen_at, dict) else [])
                if str(key).strip() and isinstance(value, str) and value.strip()
            },
            "read_at": {
                str(key): str(value)
                for key, value in (read_at.items() if isinstance(read_at, dict) else [])
                if str(key).strip() and isinstance(value, str) and value.strip()
            },
        }

    def _ensure_item_state(self, *, seed_existing_items: bool) -> None:
        self._normalize_item_state()
        baseline = self._snapshot.get("last_completed_at") or self._snapshot.get("generated_at") or utcnow().isoformat()
        changed = False
        has_existing_items = bool(self._current_identities())
        is_uninitialized_state = (
            not self._snapshot["item_state"]["seen_at"]
            and not self._snapshot["item_state"]["read_at"]
        )

        if seed_existing_items and has_existing_items and is_uninitialized_state:
            for identity in self._current_identities():
                self._snapshot["item_state"]["seen_at"][identity] = baseline
                self._snapshot["item_state"]["read_at"][identity] = baseline
            for item in self._snapshot.get("companies", []):
                if isinstance(item, dict):
                    item["is_new"] = False
            for item in self._snapshot.get("rumors", []):
                if isinstance(item, dict):
                    item["is_new"] = False
            self._write_cache()
            return

        for identity in self._current_identities():
            if identity not in self._snapshot["item_state"]["seen_at"]:
                self._snapshot["item_state"]["seen_at"][identity] = baseline
                changed = True
        if changed:
            self._write_cache()

    def _current_identities(self) -> set[str]:
        identities: set[str] = set()
        for item in self._snapshot.get("companies", []):
            if isinstance(item, dict):
                identity = self._company_identity(item)
                if identity:
                    identities.add(identity)
        for item in self._snapshot.get("rumors", []):
            if isinstance(item, dict):
                identity = self._rumor_identity(item)
                if identity:
                    identities.add(identity)
        return identities

    def _prune_item_state(self, *, reference_time: str) -> None:
        self._normalize_item_state()
        current_ids = self._current_identities()
        cutoff = self._sort_timestamp(reference_time) - timedelta(days=ITEM_STATE_RETENTION_DAYS)
        for bucket in ("seen_at", "read_at"):
            state_bucket = self._snapshot["item_state"][bucket]
            stale_ids = [
                identity
                for identity, stored_at in state_bucket.items()
                if identity not in current_ids and self._sort_timestamp(stored_at) < cutoff
            ]
            for identity in stale_ids:
                state_bucket.pop(identity, None)


def build_news_service() -> NewsService:
    encryption_key_file = os.getenv("CODEX_LB_ENCRYPTION_KEY_FILE")
    if encryption_key_file:
        project_root = Path(encryption_key_file).expanduser().resolve().parent.parent
    else:
        project_root = Path.cwd().resolve()
    cache_file = Path(os.getenv("CODEX_LB_NEWS_CACHE_FILE", project_root / "var" / "news-cache.json")).expanduser()
    refresh_seconds = int(os.getenv("CODEX_LB_NEWS_REFRESH_SECONDS", str(6 * 60 * 60)))
    initial_delay_seconds = int(os.getenv("CODEX_LB_NEWS_INITIAL_DELAY_SECONDS", "3"))
    return NewsService(
        project_root=project_root,
        cache_file=cache_file,
        refresh_interval_seconds=refresh_seconds,
        initial_delay_seconds=initial_delay_seconds,
    )
