from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Literal

import aiohttp

from app.core.config.settings import Settings, get_settings

EgressRoute = Literal["direct", "proxy"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EgressSelection:
    route: EgressRoute
    proxy_url: str | None = None


@dataclass(frozen=True, slots=True)
class EgressSnapshot:
    mode: str
    selected_route: EgressRoute
    proxy_configured: bool
    direct_ok: bool | None
    proxy_ok: bool | None
    direct_latency_ms: int | None
    proxy_latency_ms: int | None
    direct_failure_streak: int
    direct_success_streak: int
    last_switch_at: float | None
    last_probe_at: float | None


class UpstreamEgressRuntime:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._selected_route: EgressRoute = "direct"
        self._direct_ok: bool | None = None
        self._proxy_ok: bool | None = None
        self._direct_latency_ms: int | None = None
        self._proxy_latency_ms: int | None = None
        self._direct_failure_streak = 0
        self._direct_success_streak = 0
        self._proxy_failure_streak = 0
        self._proxy_success_streak = 0
        self._last_switch_at: float | None = None
        self._last_probe_at: float | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._settings.upstream_egress_mode != "auto":
            return
        if self._settings.upstream_proxy_url is None:
            logger.info("Upstream egress auto mode has no proxy URL configured; direct route remains active")
            return
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop(), name="codex-lb-upstream-egress-probe")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    def select(self) -> EgressSelection:
        mode = self._settings.upstream_egress_mode
        if mode == "proxy":
            return EgressSelection(route="proxy", proxy_url=self._settings.upstream_proxy_url)
        if mode == "direct":
            return EgressSelection(route="direct")
        if self._selected_route == "proxy" and self._settings.upstream_proxy_url:
            return EgressSelection(route="proxy", proxy_url=self._settings.upstream_proxy_url)
        return EgressSelection(route="direct")

    async def probe_once(self) -> EgressSnapshot:
        direct_ok, direct_latency_ms = await self._probe(proxy_url=None)
        proxy_ok = None
        proxy_latency_ms = None
        if self._settings.upstream_proxy_url:
            proxy_ok, proxy_latency_ms = await self._probe(proxy_url=self._settings.upstream_proxy_url)
        async with self._lock:
            self._apply_probe_result(
                direct_ok=direct_ok,
                proxy_ok=proxy_ok,
                direct_latency_ms=direct_latency_ms,
                proxy_latency_ms=proxy_latency_ms,
                now=time.monotonic(),
            )
            return self.snapshot()

    def snapshot(self) -> EgressSnapshot:
        return EgressSnapshot(
            mode=self._settings.upstream_egress_mode,
            selected_route=self._selected_route,
            proxy_configured=self._settings.upstream_proxy_url is not None,
            direct_ok=self._direct_ok,
            proxy_ok=self._proxy_ok,
            direct_latency_ms=self._direct_latency_ms,
            proxy_latency_ms=self._proxy_latency_ms,
            direct_failure_streak=self._direct_failure_streak,
            direct_success_streak=self._direct_success_streak,
            last_switch_at=self._last_switch_at,
            last_probe_at=self._last_probe_at,
        )

    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.probe_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Upstream egress probe failed unexpectedly", exc_info=True)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._settings.upstream_egress_probe_interval_seconds,
                )
            except TimeoutError:
                continue

    async def _probe(self, *, proxy_url: str | None) -> tuple[bool, int]:
        started_at = time.perf_counter()
        timeout = aiohttp.ClientTimeout(total=self._settings.upstream_egress_probe_timeout_seconds)
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                async with session.get(
                    self._settings.upstream_egress_probe_url,
                    proxy=proxy_url,
                    allow_redirects=False,
                ) as response:
                    if proxy_url is None:
                        return True, _elapsed_ms(started_at)
                    return response.status < 500, _elapsed_ms(started_at)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return False, _elapsed_ms(started_at)

    def _apply_probe_result(
        self,
        *,
        direct_ok: bool,
        proxy_ok: bool | None,
        direct_latency_ms: int | None = None,
        proxy_latency_ms: int | None = None,
        now: float,
    ) -> None:
        self._last_probe_at = now
        self._direct_ok = direct_ok
        self._proxy_ok = proxy_ok
        self._direct_latency_ms = direct_latency_ms
        self._proxy_latency_ms = proxy_latency_ms
        if direct_ok:
            self._direct_success_streak += 1
            self._direct_failure_streak = 0
        else:
            self._direct_failure_streak += 1
            self._direct_success_streak = 0
        if proxy_ok is True:
            self._proxy_success_streak += 1
            self._proxy_failure_streak = 0
        elif proxy_ok is False:
            self._proxy_failure_streak += 1
            self._proxy_success_streak = 0
        else:
            self._proxy_failure_streak = 0
            self._proxy_success_streak = 0

        if self._settings.upstream_egress_mode != "auto":
            return

        if self._selected_route == "direct":
            if not self._settings.upstream_proxy_url or proxy_ok is not True:
                return
            if self._direct_failure_streak < self._settings.upstream_egress_fail_threshold:
                return
            if self._proxy_success_streak < self._settings.upstream_egress_recover_threshold:
                return
            if not self._cooldown_elapsed(now):
                return
            self._switch("proxy", now=now)
            return

        if self._selected_route == "proxy":
            if self._direct_success_streak < self._settings.upstream_egress_recover_threshold:
                return
            if not self._cooldown_elapsed(now):
                return
            self._switch("direct", now=now)

    def _cooldown_elapsed(self, now: float) -> bool:
        if self._last_switch_at is None:
            return True
        return now - self._last_switch_at >= self._settings.upstream_egress_cooldown_seconds

    def _switch(self, route: EgressRoute, *, now: float) -> None:
        if route == self._selected_route:
            return
        logger.warning("Switching upstream egress route from %s to %s", self._selected_route, route)
        self._selected_route = route
        self._last_switch_at = now


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((time.perf_counter() - started_at) * 1000))


_runtime: UpstreamEgressRuntime | None = None


async def init_upstream_egress_runtime() -> UpstreamEgressRuntime:
    global _runtime
    runtime = UpstreamEgressRuntime(get_settings())
    _runtime = runtime
    await runtime.start()
    return runtime


async def close_upstream_egress_runtime() -> None:
    global _runtime
    runtime = _runtime
    if runtime is None:
        return
    await runtime.stop()
    _runtime = None


def get_upstream_egress_runtime() -> UpstreamEgressRuntime | None:
    return _runtime


def select_upstream_egress() -> EgressSelection:
    runtime = _runtime
    if runtime is not None:
        return runtime.select()
    settings = get_settings()
    if settings.upstream_egress_mode == "proxy":
        return EgressSelection(route="proxy", proxy_url=settings.upstream_proxy_url)
    return EgressSelection(route="direct")
