from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal as signal_module
import tempfile
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.core.config.settings import get_settings
from app.core.utils.time import utcnow
from app.modules.codex_reset_forecast.schemas import (
    CodexResetCollectionStatus,
    CodexResetEvidence,
    CodexResetForecastResponse,
    CodexResetHistoricalExample,
    CodexResetScoreFactor,
    CodexResetXActivityItem,
    CodexResetXItemKind,
    CodexResetXItemRelevance,
    ForecastConfidence,
    ProbabilityLevel,
    ResetSignalKind,
)
from app.modules.refresh_api_key import load_codex_lb_refresh_api_key

logger = logging.getLogger(__name__)

FORECAST_HORIZON_HOURS = 24
MODEL_VERSION = "codex-reset-heuristic-2026-05-29"
MANUAL_SIGNALS_ENV = "CODEX_RESET_FORECAST_SIGNALS_JSON"
SIGNAL_RETENTION_HOURS = 72
RESET_BOUNDARY_CONTEXT_RETENTION_HOURS = 14 * 24
DEFAULT_REFRESH_INTERVAL_SECONDS = 12 * 60 * 60
WORKER_DETAIL_LOG_LIMIT = 4000
PUBLIC_REFRESH_ERROR_LIMIT = 240


@dataclass(frozen=True, slots=True)
class SignalProfile:
    label: str
    base_score: float


class ManualSignal(BaseModel):
    kind: ResetSignalKind
    observed_at: datetime
    summary: str = Field(min_length=1)
    url: str | None = None
    source: str = "manual"


class CollectedXItem(BaseModel):
    class ParentItem(BaseModel):
        author_handle: str = Field(min_length=1)
        text: str = Field(min_length=1)
        translated_text_zh: str | None = None
        url: str | None = None

    author_handle: str = Field(min_length=1)
    kind: CodexResetXItemKind
    observed_at: datetime
    text: str = Field(min_length=1)
    translated_text_zh: str | None = None
    url: str = Field(min_length=1)
    reply_to: str | None = None
    parent: ParentItem | None = None
    reset_relevance: CodexResetXItemRelevance
    relevance_summary: str = Field(min_length=1)


class SignalRefreshOutput(BaseModel):
    signals: list[ManualSignal] = Field(default_factory=list, max_length=8)
    latest_items: list[CollectedXItem] = Field(default_factory=list, max_length=12)


SIGNAL_REFRESH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "signals": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": [
                            "explicit_reset_announcement",
                            "tibo_ok_request",
                            "sam_trigger",
                            "issue_acknowledged",
                            "limit_fix",
                            "confirmed_reset",
                        ],
                    },
                    "observed_at": {"type": "string"},
                    "summary": {"type": "string"},
                    "url": {"type": ["string", "null"]},
                    "source": {"type": "string"},
                },
                "required": ["kind", "observed_at", "summary", "url", "source"],
                "additionalProperties": False,
            },
        },
        "latest_items": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "properties": {
                    "author_handle": {"type": "string"},
                    "kind": {"type": "string", "enum": ["post", "reply", "quote"]},
                    "observed_at": {"type": "string"},
                    "text": {"type": "string"},
                    "translated_text_zh": {"type": "string"},
                    "url": {"type": "string"},
                    "reply_to": {"type": ["string", "null"]},
                    "parent": {
                        "type": ["object", "null"],
                        "properties": {
                            "author_handle": {"type": "string"},
                            "text": {"type": "string"},
                            "translated_text_zh": {"type": "string"},
                            "url": {"type": ["string", "null"]},
                        },
                        "required": ["author_handle", "text", "translated_text_zh", "url"],
                        "additionalProperties": False,
                    },
                    "reset_relevance": {"type": "string", "enum": ["none", "weak", "strong"]},
                    "relevance_summary": {"type": "string"},
                },
                "required": [
                    "author_handle",
                    "kind",
                    "observed_at",
                    "text",
                    "translated_text_zh",
                    "url",
                    "reply_to",
                    "parent",
                    "reset_relevance",
                    "relevance_summary",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["signals", "latest_items"],
    "additionalProperties": False,
}

SIGNAL_PROFILES: dict[ResetSignalKind, SignalProfile] = {
    "explicit_reset_announcement": SignalProfile("Explicit reset announcement", 0.86),
    "tibo_ok_request": SignalProfile("Tibo OK on reset request", 0.68),
    "sam_trigger": SignalProfile("Sam-triggered reset prompt", 0.74),
    "issue_acknowledged": SignalProfile("Limits issue acknowledged", 0.54),
    "limit_fix": SignalProfile("Limits fix or waiver", 0.38),
    "confirmed_reset": SignalProfile("Latest confirmed reset boundary", 0.0),
    "no_active_signal": SignalProfile("No active public signal", 0.0),
}

HISTORICAL_EXAMPLES: tuple[CodexResetHistoricalExample, ...] = (
    CodexResetHistoricalExample(
        label="May 17 full reset",
        classification="explicit announcement",
        signal_at=datetime(2026, 5, 16, 0, 31, 50),
        reset_at=datetime(2026, 5, 16, 17, 51, 3),
        lead_time_hours=17.32,
        signal_summary="Tibo said he would reset usage limits that evening after fixed Codex usage issues.",
        reset_summary="Tibo confirmed Codex usage limits had been reset across all paid plans.",
        signal_url="https://x.com/thsottiaux/status/2055446089957036402",
        reset_url="https://x.com/thsottiaux/status/2055707616605835333",
    ),
    CodexResetHistoricalExample(
        label="May 20 Sam/Tibo reset",
        classification="short informal trigger",
        signal_at=datetime(2026, 5, 19, 18, 31, 16),
        reset_at=datetime(2026, 5, 19, 21, 34, 37),
        lead_time_hours=3.06,
        signal_summary='Sam posted: "if this tweet gets 1 like, tibo will reset codex rate limits".',
        reset_summary='Tibo replied by quote-posting that he "went and did the thing".',
        signal_url="https://x.com/sama/status/2056804900017947046",
        reset_url="https://x.com/thsottiaux/status/2056851041132696025",
    ),
    CodexResetHistoricalExample(
        label="May 24 all-account reset",
        classification="reply precursor",
        signal_at=datetime(2026, 5, 23, 0, 21, 33),
        reset_at=datetime(2026, 5, 23, 20, 14, 35),
        lead_time_hours=19.88,
        signal_summary='Tibo replied "OK" to a direct PLEASEEEE/RESETTTT request.',
        reset_summary="Tibo confirmed usage limits were reset for all accounts after a cache-hit issue was fixed.",
        signal_url="https://x.com/thsottiaux/status/2057980213854921096",
        reset_url="https://x.com/thsottiaux/status/2058280452851638313",
    ),
)


@dataclass(slots=True)
class CodexResetForecastService:
    project_root: Path | None = None
    cache_file: Path | None = None
    refresh_interval_seconds: int = DEFAULT_REFRESH_INTERVAL_SECONDS
    initial_delay_seconds: int = 5
    refresh_enabled: bool = False
    _snapshot: dict[str, Any] = field(default_factory=dict)
    _loop_task: asyncio.Task[None] | None = None
    _refresh_task: asyncio.Task[None] | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _refresh_processes: set[asyncio.subprocess.Process] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.refresh_interval_seconds = max(60, int(self.refresh_interval_seconds))
        self.initial_delay_seconds = max(0, int(self.initial_delay_seconds))
        if self.project_root is None:
            self.project_root = Path.cwd().resolve()
        if self.cache_file is not None:
            self.cache_file = self.cache_file.expanduser()
        self._snapshot = self._empty_snapshot()

    async def start(self) -> None:
        self._load_cache()
        if not self.refresh_enabled:
            return
        if self._loop_task and not self._loop_task.done():
            return
        self._stop.clear()
        self._loop_task = asyncio.create_task(self._run_loop(), name="codex-lb-reset-forecast-loop")

    async def stop(self) -> None:
        self._stop.set()
        tasks = [task for task in (self._loop_task, self._refresh_task) if task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for process in list(self._refresh_processes):
            await self._kill_process_group(process)
        self._loop_task = None
        self._refresh_task = None

    async def request_refresh(self, *, force: bool = False) -> bool:
        if not self.refresh_enabled and not force:
            return False
        if self._refresh_task is not None and not self._refresh_task.done():
            return False
        self._refresh_task = asyncio.create_task(self._refresh(force=force), name="codex-lb-reset-forecast-refresh")
        return True

    def get_forecast(self, now: datetime | None = None) -> CodexResetForecastResponse:
        generated_at = _to_utc_naive(now) if now is not None else utcnow()
        current_cycle_signals, latest_confirmed_reset = _current_cycle_signals(
            _dedupe_signals(
                [
                    *self._load_manual_signals(generated_at),
                    *self._load_cached_signals(generated_at),
                ]
            )
        )
        evidence = self._build_evidence(
            current_cycle_signals,
            generated_at,
            latest_confirmed_reset=latest_confirmed_reset,
        )
        probability = self._score_probability(evidence)
        collection_status = self._collection_status(generated_at)

        return CodexResetForecastResponse(
            generated_at=generated_at,
            horizon_hours=FORECAST_HORIZON_HOURS,
            probability=probability,
            probability_percent=round(probability * 100),
            probability_level=_probability_level(probability),
            confidence=_confidence(evidence),
            summary=_summary(probability, evidence, collection_status),
            model_version=MODEL_VERSION,
            source_note=_source_note(collection_status),
            collection_status=collection_status,
            current_evidence=evidence,
            latest_x_items=self._load_latest_x_items(generated_at),
            score_factors=_score_factors(probability, evidence),
            historical_examples=list(HISTORICAL_EXAMPLES),
        )

    async def _run_loop(self) -> None:
        first_iteration = True
        while not self._stop.is_set():
            delay_seconds = self._seconds_until_next_refresh_attempt(utcnow())
            if first_iteration and delay_seconds <= 0:
                delay_seconds = self.initial_delay_seconds
            first_iteration = False
            if delay_seconds > 0:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay_seconds)
                    return
                except asyncio.TimeoutError:
                    pass
            if self._stop.is_set():
                return
            await self.request_refresh(force=False)
            refresh_task = self._refresh_task
            if refresh_task is not None:
                stop_task = asyncio.create_task(self._stop.wait())
                try:
                    await asyncio.wait({refresh_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    if not stop_task.done():
                        stop_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await stop_task
                if self._stop.is_set():
                    return
                with contextlib.suppress(Exception):
                    refresh_task.result()

    def _seconds_until_next_refresh_attempt(self, now: datetime) -> float:
        reference_at = self._refresh_attempt_reference_at()
        if reference_at is None:
            return 0.0
        next_refresh_due_at = reference_at + timedelta(seconds=self.refresh_interval_seconds)
        return max(0.0, (next_refresh_due_at - _to_utc_naive(now)).total_seconds())

    async def _refresh(self, *, force: bool) -> None:
        async with self._refresh_lock:
            started_at = utcnow().isoformat()
            self._snapshot["refresh_in_progress"] = True
            self._snapshot["last_started_at"] = started_at
            self._snapshot["last_error"] = None
            self._write_cache()
            try:
                api_key = await self._load_codex_lb_api_key()
                if not api_key:
                    raise RuntimeError("Reset forecast refresh is not configured with a usable codex-lb API key.")
                payload = await self._run_codex_job(api_key=api_key, prompt=self._build_live_signal_prompt())
                refresh_output = SignalRefreshOutput.model_validate(payload)
                if not refresh_output.latest_items:
                    raise RuntimeError("Reset forecast worker returned no inspected X items.")
                completed_at = utcnow().isoformat()
                self._snapshot.update(
                    {
                        "signals": [_signal_to_cache_item(signal) for signal in refresh_output.signals],
                        "latest_items": [_x_item_to_cache_item(item) for item in refresh_output.latest_items],
                        "refresh_in_progress": False,
                        "last_completed_at": completed_at,
                        "last_error": None,
                    }
                )
            except asyncio.CancelledError:
                self._snapshot["refresh_in_progress"] = False
                self._write_cache()
                raise
            except Exception as exc:
                logger.warning("Codex reset forecast refresh failed force=%s", force, exc_info=True)
                self._snapshot["refresh_in_progress"] = False
                self._snapshot["last_error"] = _public_refresh_error(exc)
            self._write_cache()

    async def _run_codex_job(self, *, api_key: str, prompt: str) -> dict[str, Any]:
        assert self.project_root is not None
        with tempfile.TemporaryDirectory(prefix="codex-lb-reset-forecast-") as tmpdir_name:
            tmpdir = Path(tmpdir_name)
            schema_path = tmpdir / "schema.json"
            output_path = tmpdir / "signals.json"
            schema_path.write_text(json.dumps(SIGNAL_REFRESH_SCHEMA), encoding="utf-8")

            env = os.environ.copy()
            env["OPENAI_API_KEY"] = api_key
            args = [
                *_build_codex_command_prefix(self.project_root),
                "exec",
                "--skip-git-repo-check",
                "-C",
                str(self.project_root),
                "-c",
                'preferred_auth_method="apikey"',
                "-c",
                'model_provider="codex-lb"',
                "-c",
                'model_providers.codex-lb.env_key="OPENAI_API_KEY"',
                "-c",
                f'model_reasoning_effort="{os.getenv("CODEX_LB_CODEX_RESET_FORECAST_REASONING", "low")}"',
                "-m",
                os.getenv("CODEX_LB_CODEX_RESET_FORECAST_MODEL", "gpt-5.5"),
                "--output-schema",
                str(schema_path),
                "-o",
                str(output_path),
                prompt,
            ]
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(self.project_root),
                env=env,
                start_new_session=True,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._refresh_processes.add(process)
            timeout_seconds = int(os.getenv("CODEX_LB_CODEX_RESET_FORECAST_JOB_TIMEOUT_SECONDS", "600"))
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
            except asyncio.CancelledError:
                if process.returncode is None:
                    await self._kill_process_group(process)
                raise
            except asyncio.TimeoutError:
                await self._kill_process_group(process)
                raise RuntimeError("reset forecast worker timed out while waiting for Codex.") from None
            finally:
                self._refresh_processes.discard(process)

            if process.returncode != 0:
                detail = (stderr or stdout).decode("utf-8", errors="ignore").strip()
                detail = detail[-WORKER_DETAIL_LOG_LIMIT:] if detail else "Codex exited without details."
                logger.warning(
                    "Codex reset forecast worker exited with status %s: %s",
                    process.returncode,
                    detail,
                )
                raise RuntimeError(_worker_failure_summary(detail))

            try:
                return json.loads(output_path.read_text(encoding="utf-8"))
            except Exception as exc:
                detail = stdout.decode("utf-8", errors="ignore").strip()[-600:]
                logger.warning("Codex reset forecast worker returned invalid JSON: %s", detail)
                raise RuntimeError("Reset forecast worker returned invalid JSON.") from exc

    def _build_live_signal_prompt(self) -> str:
        now = utcnow()
        return textwrap.dedent(
            f"""
            Inspect recent X activity for OpenAI Codex usage-limit reset-cycle signals and return strict JSON.

            Current UTC date: {now.date().isoformat()}.
            Current UTC timestamp: {now.isoformat()}.

            You must use the configured url-fetcher MCP against x.com during this task.
            The required MCP calls are:
            - mcp__url_fetcher.x_paginate_timeline for username=thsottiaux, timeline=posts
            - mcp__url_fetcher.x_get_user_replies for username=thsottiaux
            - mcp__url_fetcher.x_paginate_timeline for username=sama, timeline=posts
            - mcp__url_fetcher.x_get_user_replies for username=sama
            Do not answer from memory, search, or assumptions. Do not return final JSON until these X MCP calls
            have completed or the MCP tool explicitly reports an error. If a tool errors, continue with the other
            required X MCP calls and populate latest_items from any visible X items that were returned.

            Focus on the last 72 hours, with special attention to the last 24 hours.
            Populate latest_items with the most recent visible posts/replies/quotes you inspected, even when no reset
            signal is found. Prefer 4 to 6 latest_items total, newest first where practical, mixing Tibo and Sam when
            both are visible. You must include the latest visible deterministic reset confirmation as one
            confirmed_reset signal and one latest_item when it is visible and needed to explain the current reset
            cycle, even if it is slightly older than 72 hours.

            Evidence text policy:
            - For reset-relevant latest_items, keep latest_items.text as the concise visible English original from X.
              Reset-relevant means reset_relevance is weak or strong, or the item is the latest confirmed_reset
              boundary.
            - For unrelated latest_items, do not reproduce the source text. Write a neutral English paraphrase under
              120 characters.
            - For translated_text_zh, provide a faithful Simplified Chinese translation for reset-relevant quoted
              items; for unrelated items, translate the short paraphrase.
            Write latest_items.relevance_summary in Simplified Chinese.

            Include only signals that plausibly relate to Codex usage-limit resets, rate limits, quota resets,
            usage caps, temporary waivers, fixes that imply a future reset, completed reset confirmations, or
            Tibo/Sam replies that match prior reset precursors.

            Treat already-completed resets as cycle boundaries. Before returning precursor signals, identify the
            latest visible deterministic confirmation that Codex usage limits have already been reset. If such a
            confirmation exists, include it as one confirmed_reset signal, and include only future-reset precursor
            signals observed strictly after that confirmed_reset timestamp. Do not include precursor signals that
            happened before or at the latest confirmed_reset, because the forecast is for the next reset after the
            latest completed reset.

            Signal kind rules:
            - explicit_reset_announcement: direct claim that Codex limits will be reset or are about to be reset
            - tibo_ok_request: Tibo replies OK / yes / similar to a direct reset/rate-limit request
            - sam_trigger: Sam nudges or jokes that Tibo will reset Codex limits
            - issue_acknowledged: Tibo/Sam acknowledges a Codex limit/cap/quota problem
            - limit_fix: Tibo/Sam says a relevant limit/cache/quota bug was fixed or waived
            - confirmed_reset: deterministic confirmation that a Codex usage-limit reset already happened, such as
              "limits have been reset", "went and did the thing", or equivalent completed-action wording
            Do not classify already-completed reset actions as explicit_reset_announcement.

            Return an empty signals array if no matching recent signal is visible.
            For every signal, include the direct x.com status URL and the visible post timestamp in ISO 8601 UTC.
            For every latest item, include the direct x.com status URL, visible timestamp in ISO 8601 UTC, whether it
            is a post/reply/quote, a reply_to handle or label when visible, and reset_relevance:
            - none: unrelated to Codex limits or resets
            - weak: mentions Codex limits, usage, quota, cache, or reset context indirectly
            - strong: directly resembles a prior reset precursor
            For replies or quotes, populate parent only when that parent/context is necessary to understand a
            Codex usage-limit reset signal. For parent on reset-relevant items, include author_handle, concise visible
            English original text, Simplified Chinese translation, and direct URL when visible. Use parent=null for
            unrelated replies/quotes or when the parent is not visible.
            Do not include the older May 17, May 20, or May 24 examples unless they are newly visible within the
            last 72 hours.
            Do not infer a signal from unrelated Codex product discussion.
            Return JSON only, matching the provided schema.
            """
        ).strip()

    async def _load_codex_lb_api_key(self) -> str | None:
        return await load_codex_lb_refresh_api_key(
            value_env_keys=("CODEX_LB_CODEX_RESET_FORECAST_API_KEY",),
            name_env_keys=("CODEX_LB_CODEX_RESET_FORECAST_API_KEY_NAME",),
        )

    async def _kill_process_group(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal_module.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()

    def _load_manual_signals(self, now: datetime) -> list[ManualSignal]:
        raw = os.getenv(MANUAL_SIGNALS_ENV, "").strip()
        if not raw:
            return []
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []

        signals: list[ManualSignal] = []
        for item in payload:
            try:
                signal = ManualSignal.model_validate(item)
            except ValidationError:
                continue
            if _age_hours(signal.observed_at, now) <= _signal_retention_hours(signal):
                signals.append(signal)
        return signals

    def _load_cached_signals(self, now: datetime) -> list[ManualSignal]:
        signals: list[ManualSignal] = []
        for item in self._snapshot.get("signals", []):
            if not isinstance(item, dict):
                continue
            try:
                signal = ManualSignal.model_validate(item)
            except ValidationError:
                continue
            if _age_hours(signal.observed_at, now) <= _signal_retention_hours(signal):
                signals.append(signal)
        return signals

    def _load_latest_x_items(self, now: datetime) -> list[CodexResetXActivityItem]:
        items: list[CodexResetXActivityItem] = []
        for item in self._snapshot.get("latest_items", []):
            if not isinstance(item, dict):
                continue
            try:
                collected = CollectedXItem.model_validate(item)
            except ValidationError:
                continue
            if _age_hours(collected.observed_at, now) > _x_item_retention_hours(collected):
                continue
            items.append(
                CodexResetXActivityItem(
                    author_handle=collected.author_handle,
                    kind=collected.kind,
                    observed_at=_to_utc_naive(collected.observed_at),
                    text=collected.text,
                    translated_text_zh=collected.translated_text_zh or collected.text,
                    url=collected.url,
                    reply_to=collected.reply_to,
                    parent=_to_response_parent_item(collected.parent),
                    reset_relevance=collected.reset_relevance,
                    relevance_summary=collected.relevance_summary,
                )
            )
        return sorted(items, key=lambda item: item.observed_at, reverse=True)[:12]

    def _build_evidence(
        self,
        signals: list[ManualSignal],
        now: datetime,
        *,
        latest_confirmed_reset: ManualSignal | None = None,
    ) -> list[CodexResetEvidence]:
        evidence: list[CodexResetEvidence] = []
        for signal in signals:
            age = _age_hours(signal.observed_at, now)
            profile = SIGNAL_PROFILES[signal.kind]
            evidence.append(
                CodexResetEvidence(
                    kind=signal.kind,
                    label=profile.label,
                    summary=signal.summary,
                    observed_at=_to_utc_naive(signal.observed_at),
                    url=signal.url,
                    source=signal.source,
                    age_hours=round(age, 2),
                    score=round(profile.base_score * _freshness_multiplier(age), 3),
                )
            )

        if evidence:
            if latest_confirmed_reset is not None:
                evidence.append(_confirmed_reset_evidence(latest_confirmed_reset, now))
            return sorted(evidence, key=lambda item: item.score, reverse=True)

        if latest_confirmed_reset is not None:
            reset_time = _to_utc_naive(latest_confirmed_reset.observed_at).isoformat()
            return [
                _confirmed_reset_evidence(latest_confirmed_reset, now),
                CodexResetEvidence(
                    kind="no_active_signal",
                    label=SIGNAL_PROFILES["no_active_signal"].label,
                    summary=f"No Tibo/Sam reset precursor is active after the latest confirmed reset at {reset_time}Z.",
                    source="post-reset scoring window",
                    score=0.0,
                ),
            ]

        return [
            CodexResetEvidence(
                kind="no_active_signal",
                label=SIGNAL_PROFILES["no_active_signal"].label,
                summary="No Tibo/Sam reset precursor is active in the current scoring window.",
                source="live collector cache" if self.refresh_enabled else "local heuristic",
                score=0.0,
            )
        ]

    def _score_probability(self, evidence: list[CodexResetEvidence]) -> float:
        baseline = 0.08
        best_signal_score = max((item.score for item in evidence), default=0.0)
        if best_signal_score <= 0:
            return baseline
        clustered_signal_bonus = min(0.08, 0.02 * max(0, len([item for item in evidence if item.score > 0]) - 1))
        return round(min(0.92, max(baseline, best_signal_score + clustered_signal_bonus)), 3)

    def _collection_status(self, now: datetime) -> CodexResetCollectionStatus:
        last_started_at = _parse_snapshot_datetime(self._snapshot.get("last_started_at"))
        last_completed_at = _parse_snapshot_datetime(self._snapshot.get("last_completed_at"))
        next_refresh_due_at = None
        if self.refresh_enabled:
            refresh_reference_at = self._refresh_attempt_reference_at()
            if refresh_reference_at is None:
                next_refresh_due_at = now
            else:
                next_refresh_due_at = refresh_reference_at + timedelta(seconds=self.refresh_interval_seconds)
        return CodexResetCollectionStatus(
            refresh_enabled=self.refresh_enabled,
            refresh_in_progress=bool(self._snapshot.get("refresh_in_progress")),
            last_started_at=last_started_at,
            last_completed_at=last_completed_at,
            last_error=_optional_str(self._snapshot.get("last_error")),
            next_refresh_due_at=next_refresh_due_at,
        )

    def _refresh_attempt_reference_at(self) -> datetime | None:
        candidates = [
            value
            for value in (
                _parse_snapshot_datetime(self._snapshot.get("last_completed_at")),
                _parse_snapshot_datetime(self._snapshot.get("last_started_at")),
            )
            if value is not None
        ]
        return max(candidates, default=None)

    def _empty_snapshot(self) -> dict[str, Any]:
        return {
            "signals": [],
            "latest_items": [],
            "refresh_in_progress": False,
            "last_started_at": None,
            "last_completed_at": None,
            "last_error": None,
        }

    def _load_cache(self) -> None:
        if self.cache_file is None or not self.cache_file.is_file():
            return
        try:
            payload = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Failed to read Codex reset forecast cache", exc_info=True)
            return
        if not isinstance(payload, dict):
            return
        snapshot = self._empty_snapshot()
        for key in snapshot:
            if key in payload:
                snapshot[key] = payload[key]
        snapshot["refresh_in_progress"] = False
        self._snapshot = snapshot

    def _write_cache(self) -> None:
        if self.cache_file is None:
            return
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text(json.dumps(self._snapshot, ensure_ascii=False, indent=2), encoding="utf-8")


def _to_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _age_hours(observed_at: datetime, now: datetime) -> float:
    observed = _to_utc_naive(observed_at)
    current = _to_utc_naive(now)
    return max(0.0, (current - observed).total_seconds() / 3600)


def _signal_retention_hours(signal: ManualSignal) -> int:
    if _is_confirmed_reset_boundary(signal):
        return RESET_BOUNDARY_CONTEXT_RETENTION_HOURS
    return SIGNAL_RETENTION_HOURS


def _x_item_retention_hours(item: CollectedXItem) -> int:
    if item.reset_relevance == "strong":
        return RESET_BOUNDARY_CONTEXT_RETENTION_HOURS
    return SIGNAL_RETENTION_HOURS


def _freshness_multiplier(age_hours: float) -> float:
    if age_hours <= 6:
        return 1.0
    if age_hours <= 24:
        return 1.0 - ((age_hours - 6) / 18 * 0.45)
    if age_hours <= 36:
        return 0.55 - ((age_hours - 24) / 12 * 0.40)
    return 0.0


def _probability_level(probability: float) -> ProbabilityLevel:
    if probability >= 0.65:
        return "high"
    if probability >= 0.30:
        return "elevated"
    return "low"


def _confidence(evidence: list[CodexResetEvidence]) -> ForecastConfidence:
    if any(item.kind == "explicit_reset_announcement" and item.score >= 0.5 for item in evidence):
        return "high"
    if any(item.score > 0 for item in evidence):
        return "medium"
    return "medium"


def _summary(
    probability: float,
    evidence: list[CodexResetEvidence],
    collection_status: CodexResetCollectionStatus,
) -> str:
    if probability >= 0.65:
        return "Recent public signals closely match prior Codex reset precursors."
    if probability >= 0.30:
        return "Some reset-like signals are active, but the pattern is not as strong as prior confirmed resets."
    if collection_status.last_error and collection_status.last_completed_at is None:
        return "Low near-term reset odds, but the live collector has not completed successfully yet."
    if any(item.kind == "confirmed_reset" for item in evidence) and not any(item.score > 0 for item in evidence):
        return (
            "Low near-term reset odds because scoring starts after the latest confirmed reset "
            "and no newer precursor is active."
        )
    if any(item.kind == "no_active_signal" for item in evidence):
        return "Low near-term reset odds because no active Tibo/Sam precursor is present in the scoring input."
    return "Low near-term reset odds; available signals are stale or weak."


def _source_note(collection_status: CodexResetCollectionStatus) -> str:
    base = "Deterministic heuristic seeded from confirmed May 2026 Tibo/Sam reset examples."
    if collection_status.refresh_enabled:
        if collection_status.last_completed_at is None:
            return f"{base} Live X collection is enabled but has not completed yet."
        return f"{base} Live X collection is enabled and refreshes the Tibo/Sam signal cache hourly by default."
    return f"{base} Live X collection is disabled for this service instance."


def _score_factors(probability: float, evidence: list[CodexResetEvidence]) -> list[CodexResetScoreFactor]:
    best_signal = max((item.score for item in evidence), default=0.0)
    active_count = len([item for item in evidence if item.score > 0])
    return [
        CodexResetScoreFactor(
            key="baseline",
            label="Historical baseline",
            value=0.08,
            description="Base rate from the observed May 2026 reset cadence before any current public signal.",
        ),
        CodexResetScoreFactor(
            key="current_signal",
            label="Current public signal",
            value=round(best_signal, 3),
            description="Highest weighted recent Tibo/Sam reset-related signal supplied to the predictor.",
        ),
        CodexResetScoreFactor(
            key="signal_cluster",
            label="Signal clustering",
            value=round(min(0.08, 0.02 * max(0, active_count - 1)), 3),
            description="Small lift when multiple independent current signals point in the same direction.",
        ),
        CodexResetScoreFactor(
            key="final_probability",
            label="Final probability",
            value=probability,
            description="Estimated chance of a Codex usage-limit reset in the next 24 hours.",
        ),
    ]


def _signal_to_cache_item(signal: ManualSignal) -> dict[str, str | None]:
    return {
        "kind": signal.kind,
        "observed_at": _to_utc_naive(signal.observed_at).isoformat() + "Z",
        "summary": signal.summary,
        "url": signal.url,
        "source": signal.source,
    }


def _x_item_to_cache_item(item: CollectedXItem) -> dict[str, str | None]:
    return {
        "author_handle": item.author_handle,
        "kind": item.kind,
        "observed_at": _to_utc_naive(item.observed_at).isoformat() + "Z",
        "text": item.text,
        "translated_text_zh": item.translated_text_zh or item.text,
        "url": item.url,
        "reply_to": item.reply_to,
        "parent": _parent_to_cache_item(item.parent),
        "reset_relevance": item.reset_relevance,
        "relevance_summary": item.relevance_summary,
    }


def _parent_to_cache_item(parent: CollectedXItem.ParentItem | None) -> dict[str, str | None] | None:
    if parent is None:
        return None
    return {
        "author_handle": parent.author_handle,
        "text": parent.text,
        "translated_text_zh": parent.translated_text_zh or parent.text,
        "url": parent.url,
    }


def _to_response_parent_item(
    parent: CollectedXItem.ParentItem | None,
) -> CodexResetXActivityItem.ParentItem | None:
    if parent is None:
        return None
    return CodexResetXActivityItem.ParentItem(
        author_handle=parent.author_handle,
        text=parent.text,
        translated_text_zh=parent.translated_text_zh or parent.text,
        url=parent.url,
    )


def _dedupe_signals(signals: list[ManualSignal]) -> list[ManualSignal]:
    deduped: list[ManualSignal] = []
    seen: set[str] = set()
    for signal in signals:
        key = signal.url or f"{signal.kind}:{_to_utc_naive(signal.observed_at).isoformat()}:{signal.summary}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(signal)
    return deduped


def _current_cycle_signals(signals: list[ManualSignal]) -> tuple[list[ManualSignal], ManualSignal | None]:
    latest_confirmed_reset = max(
        (signal for signal in signals if _is_confirmed_reset_boundary(signal)),
        key=lambda signal: _to_utc_naive(signal.observed_at),
        default=None,
    )
    if latest_confirmed_reset is None:
        return [signal for signal in signals if not _is_confirmed_reset_boundary(signal)], None

    boundary_at = _to_utc_naive(latest_confirmed_reset.observed_at)
    return (
        [
            signal
            for signal in signals
            if not _is_confirmed_reset_boundary(signal) and _to_utc_naive(signal.observed_at) > boundary_at
        ],
        latest_confirmed_reset,
    )


def _is_confirmed_reset_boundary(signal: ManualSignal) -> bool:
    if signal.kind == "confirmed_reset":
        return True
    if signal.kind != "explicit_reset_announcement":
        return False

    summary = signal.summary.lower()
    completed_phrases = (
        "had been reset",
        "has been reset",
        "have been reset",
        "were reset",
        "was reset",
        "already been reset",
        "confirmed the reset",
        "confirming the reset",
        "went and did the thing",
    )
    return any(phrase in summary for phrase in completed_phrases)


def _confirmed_reset_evidence(signal: ManualSignal, now: datetime) -> CodexResetEvidence:
    age = _age_hours(signal.observed_at, now)
    return CodexResetEvidence(
        kind="confirmed_reset",
        label=SIGNAL_PROFILES["confirmed_reset"].label,
        summary=signal.summary,
        observed_at=_to_utc_naive(signal.observed_at),
        url=signal.url,
        source=signal.source,
        age_hours=round(age, 2),
        score=0.0,
    )


def _parse_snapshot_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return _to_utc_naive(datetime.fromisoformat(raw))
    except ValueError:
        return None


def _optional_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _public_refresh_error(exc: Exception) -> str:
    message = str(exc).strip()
    if not message:
        return "Reset forecast collector failed. Check backend logs for details."
    if len(message) <= PUBLIC_REFRESH_ERROR_LIMIT:
        return message
    return message[: PUBLIC_REFRESH_ERROR_LIMIT - 1].rstrip() + "..."


def _worker_failure_summary(detail: str) -> str:
    normalized = detail.lower()
    if "content_filter" in normalized:
        return (
            "Reset forecast worker response was interrupted by a content filter. "
            "The previous successful cache remains in use."
        )
    if "timed out handshaking with mcp server" in normalized or "failed to initialize mcp client" in normalized:
        return (
            "Reset forecast worker failed while starting the X MCP client. "
            "The previous successful cache remains in use."
        )
    if "stream disconnected before completion" in normalized:
        return (
            "Reset forecast worker stream disconnected before completion. The previous successful cache remains in use."
        )
    return "Reset forecast worker failed while running Codex. Check backend logs for details."


def _build_codex_command_prefix(project_root: Path) -> list[str]:
    launcher, checked_launchers = _find_first_existing_executable(_codex_launcher_candidates(project_root))
    if launcher is None:
        checked = ", ".join(checked_launchers)
        raise RuntimeError(f"Codex CLI executable was not found. Checked: {checked}")

    if not _is_node_launcher(launcher):
        return [str(launcher)]

    node, checked_nodes = _find_first_existing_executable(_node_executable_candidates(project_root))
    if node is None:
        checked = ", ".join(checked_nodes)
        raise RuntimeError(
            f"Codex CLI launcher requires Node.js but no node executable was found. "
            f"Launcher: {launcher}. Checked: {checked}"
        )
    return [str(node), str(launcher)]


def _codex_launcher_candidates(project_root: Path) -> list[Path]:
    home = _home_dir()
    candidates = [
        project_root / "var" / "bin" / "codex",
    ]
    if home is not None:
        candidates.extend(
            [
                home / ".local" / "bin" / "codex",
                home / ".bun" / "bin" / "codex",
                home / ".npm-global" / "bin" / "codex",
            ]
        )
    candidates.extend(
        [
            Path("/usr/local/bin/codex"),
            Path("/usr/bin/codex"),
            Path("/bin/codex"),
        ]
    )
    return _dedupe_paths(candidates)


def _node_executable_candidates(project_root: Path) -> list[Path]:
    home = _home_dir()
    candidates = [
        project_root / "var" / "bin" / "node",
    ]
    if home is not None:
        candidates.extend(
            [
                home / ".local" / "bin" / "node",
                home / ".bun" / "bin" / "node",
            ]
        )
        candidates.extend(sorted((home / ".nvm" / "versions" / "node").glob("*/bin/node"), reverse=True))
    candidates.extend(sorted(Path("/usr/local/nodejs").glob("node-*/bin/node"), reverse=True))
    candidates.extend(
        [
            Path("/usr/local/bin/node"),
            Path("/usr/bin/node"),
            Path("/bin/node"),
        ]
    )
    return _dedupe_paths(candidates)


def _find_first_existing_executable(candidates: list[Path]) -> tuple[Path | None, list[str]]:
    checked: list[str] = []
    for candidate in candidates:
        checked.append(str(candidate))
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate, checked
    return None, checked


def _is_node_launcher(path: Path) -> bool:
    resolved = path.resolve()
    if resolved.suffix == ".js":
        return True
    try:
        first_line = resolved.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
    except (OSError, IndexError, UnicodeDecodeError):
        return False
    return first_line.startswith("#!") and "node" in first_line


def _home_dir() -> Path | None:
    try:
        return Path.home()
    except RuntimeError:
        return None


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def build_codex_reset_forecast_service() -> CodexResetForecastService:
    settings = get_settings()
    encryption_key_file = os.getenv("CODEX_LB_ENCRYPTION_KEY_FILE")
    if encryption_key_file:
        project_root = Path(encryption_key_file).expanduser().resolve().parent.parent
    else:
        project_root = Path.cwd().resolve()
    cache_file = Path(
        os.getenv("CODEX_LB_CODEX_RESET_FORECAST_CACHE_FILE", project_root / "var" / "codex-reset-forecast-cache.json")
    ).expanduser()
    return CodexResetForecastService(
        project_root=project_root,
        cache_file=cache_file,
        refresh_interval_seconds=settings.codex_reset_forecast_refresh_interval_seconds,
        initial_delay_seconds=settings.codex_reset_forecast_initial_delay_seconds,
        refresh_enabled=settings.codex_reset_forecast_refresh_enabled,
    )
