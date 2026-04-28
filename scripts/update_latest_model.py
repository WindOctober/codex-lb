#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LATEST_MODEL_PATH = ROOT / "config" / "latest_model.json"
QUOTA_REGISTRY_PATH = ROOT / "config" / "additional_quota_registry.json"
_NORMALIZE_PATTERN = re.compile(r"[^a-z0-9]+")


def _normalize_identifier(value: str) -> str:
    normalized = _NORMALIZE_PATTERN.sub("_", value.strip().lower()).strip("_")
    if not normalized:
        raise ValueError(f"invalid identifier: {value!r}")
    return normalized


def _display_label(model_id: str) -> str:
    parts = model_id.strip().split("-")
    if not parts:
        return model_id
    return "-".join(part.upper() if part.lower() == "gpt" else part for part in parts)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _latest_registry_entry(model_id: str, quota_key: str, display_label: str) -> dict[str, Any]:
    return {
        "quota_key": quota_key,
        "display_label": display_label,
        "model_ids": [model_id],
        "quota_key_aliases": ["latest_model"],
        "limit_name_aliases": [model_id, display_label, quota_key],
        "metered_feature_aliases": [quota_key],
        "role": "latest_model",
    }


def update_latest_model(model_id: str, quota_key: str | None, display_label: str | None) -> None:
    resolved_quota_key = _normalize_identifier(quota_key or model_id)
    resolved_display_label = display_label.strip() if display_label else _display_label(model_id)

    _write_json(
        LATEST_MODEL_PATH,
        {
            "model_id": model_id,
            "quota_key": resolved_quota_key,
            "display_label": resolved_display_label,
        },
    )

    registry = _read_json(QUOTA_REGISTRY_PATH, [])
    if not isinstance(registry, list):
        raise ValueError(f"additional quota registry must be a list: {QUOTA_REGISTRY_PATH}")

    entry = _latest_registry_entry(model_id, resolved_quota_key, resolved_display_label)
    updated: list[Any] = []
    replaced = False
    for item in registry:
        if not isinstance(item, dict):
            updated.append(item)
            continue
        if item.get("role") == "latest_model" or item.get("quota_key") == resolved_quota_key:
            if not replaced:
                updated.append(entry)
                replaced = True
            continue
        updated.append(item)
    if not replaced:
        updated.append(entry)
    _write_json(QUOTA_REGISTRY_PATH, updated)


def main() -> None:
    parser = argparse.ArgumentParser(description="Update the latest-model quota configuration.")
    parser.add_argument("model_id", nargs="?", default="gpt-5.5")
    parser.add_argument("--quota-key", default=None)
    parser.add_argument("--display-label", default=None)
    args = parser.parse_args()
    update_latest_model(args.model_id, args.quota_key, args.display_label)
    print(f"latest model updated: {args.model_id}")


if __name__ == "__main__":
    main()
