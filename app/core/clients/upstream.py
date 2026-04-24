from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

import aiohttp

from app.core.clients.http import get_http_client

_PROBE_TIMEOUT_SECONDS = 8.0


class UpstreamProbeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class UpstreamProbeResult:
    base_url: str
    wire_api: str
    supported_models: tuple[str, ...]


def normalize_upstream_base_url(base_url: str) -> str:
    value = base_url.strip().rstrip("/")
    if not value:
        raise UpstreamProbeError("Base URL is required")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise UpstreamProbeError("Base URL must be an absolute http(s) URL")
    if value.endswith("/v1"):
        value = value[:-3]
    elif value.endswith("/backend-api"):
        value = value[: -len("/backend-api")]
    return value.rstrip("/")


def build_models_url(base_url: str, wire_api: str) -> str:
    normalized = normalize_upstream_base_url(base_url)
    if wire_api == "responses":
        return f"{normalized}/v1/models"
    return f"{normalized}/backend-api/models"


def build_responses_url(base_url: str, wire_api: str) -> str:
    normalized = normalize_upstream_base_url(base_url)
    if wire_api in {"responses", "v1"}:
        return f"{normalized}/v1/responses"
    if normalized.endswith("/backend-api/codex") or normalized.endswith("/codex"):
        return f"{normalized}/responses"
    if normalized.endswith("/backend-api"):
        return f"{normalized}/codex/responses"
    return f"{normalized}/backend-api/codex/responses"


def build_compact_responses_url(base_url: str) -> str:
    normalized = normalize_upstream_base_url(base_url)
    if normalized.endswith("/backend-api/codex") or normalized.endswith("/codex"):
        return f"{normalized}/responses/compact"
    if normalized.endswith("/backend-api"):
        return f"{normalized}/codex/responses/compact"
    return f"{normalized}/backend-api/codex/responses/compact"


async def probe_upstream_provider(*, base_url: str, api_key: str) -> UpstreamProbeResult:
    normalized = normalize_upstream_base_url(base_url)
    last_error: Exception | None = None
    for wire_api in ("responses", "codex"):
        try:
            models = await _fetch_models(normalized, api_key, wire_api)
            return UpstreamProbeResult(base_url=normalized, wire_api=wire_api, supported_models=tuple(models))
        except Exception as exc:
            last_error = exc
    if isinstance(last_error, UpstreamProbeError):
        raise last_error
    raise UpstreamProbeError(str(last_error) if last_error else "Failed to probe upstream provider")


async def _fetch_models(base_url: str, api_key: str, wire_api: str) -> list[str]:
    timeout = aiohttp.ClientTimeout(total=_PROBE_TIMEOUT_SECONDS)
    headers = {"Accept": "application/json", "Authorization": f"Bearer {api_key}"}
    models_url = build_models_url(base_url, wire_api)
    async with get_http_client().session.get(models_url, headers=headers, timeout=timeout) as resp:
        if resp.status >= 400:
            raise UpstreamProbeError(f"Provider models endpoint returned HTTP {resp.status}")
        try:
            payload = await resp.json(content_type=None)
        except Exception as exc:
            raise UpstreamProbeError("Provider models endpoint did not return JSON") from exc
    return _extract_model_ids(payload)


def _extract_model_ids(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return []
    entries = payload.get("data")
    if not isinstance(entries, list):
        entries = payload.get("models")
    if not isinstance(entries, list):
        return []
    models: list[str] = []
    for entry in entries:
        if isinstance(entry, dict):
            model_id = entry.get("id") or entry.get("slug")
        else:
            model_id = entry
        if isinstance(model_id, str) and model_id.strip():
            models.append(model_id.strip())
    return sorted(set(models))
