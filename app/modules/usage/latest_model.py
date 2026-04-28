from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

_NORMALIZE_PATTERN = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class LatestModelConfig:
    model_id: str
    quota_key: str
    display_label: str


def _normalize_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _NORMALIZE_PATTERN.sub("_", value.strip().lower()).strip("_")
    return normalized or None


def _default_config_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "latest_model.json"


def _config_path() -> Path:
    configured = os.environ.get("CODEX_LB_LATEST_MODEL_FILE", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return _default_config_path()


@lru_cache(maxsize=8)
def _load_latest_model_config(path_str: str) -> LatestModelConfig | None:
    path = Path(path_str)
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"latest model config must be an object: {path}")
    return _config_from_json(raw)


def _config_from_json(raw: dict[str, Any]) -> LatestModelConfig:
    model_id = str(raw.get("model_id") or raw.get("model") or "").strip()
    if not model_id:
        raise ValueError("latest model config requires model_id")
    quota_key = _normalize_identifier(str(raw.get("quota_key") or model_id))
    if quota_key is None:
        raise ValueError(f"invalid latest model quota_key: {raw.get('quota_key')!r}")
    display_label = str(raw.get("display_label") or model_id).strip() or model_id
    return LatestModelConfig(model_id=model_id, quota_key=quota_key, display_label=display_label)


def clear_latest_model_cache() -> None:
    _load_latest_model_config.cache_clear()


def get_latest_model_config() -> LatestModelConfig | None:
    return _load_latest_model_config(str(_config_path()))


def get_latest_model_quota_key() -> str | None:
    config = get_latest_model_config()
    return config.quota_key if config is not None else None


def get_latest_model_id() -> str | None:
    config = get_latest_model_config()
    return config.model_id if config is not None else None
