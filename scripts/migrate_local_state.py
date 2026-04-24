#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

LOCAL_PROVIDER_REVISION = "20260328_000000_add_upstream_provider_accounts"
REQUIRED_ACCOUNT_COLUMNS = {
    "provider_kind",
    "upstream_base_url",
    "upstream_wire_api",
    "upstream_priority",
    "supported_models_json",
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy a local codex-lb runtime state directory into this fork's runtime layout."
    )
    parser.add_argument("--source-var", default="/home/work/tools/codex-lb/var", help="Source local var directory")
    parser.add_argument("--target-var", default="var", help="Target fork var directory")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing target files")
    args = parser.parse_args()

    source_var = Path(args.source_var).expanduser().resolve()
    target_var = Path(args.target_var).expanduser().resolve()
    source_db = source_var / "store.db"
    target_db = target_var / "store.db"

    if not source_db.exists():
        raise SystemExit(f"Source database does not exist: {source_db}")

    target_var.mkdir(parents=True, exist_ok=True)
    _copy_file(source_db, target_db, overwrite=args.overwrite)
    _copy_optional(source_var / "encryption.key", target_var / "encryption.key", overwrite=args.overwrite)
    _copy_optional(source_var / "news-cache.json", target_var / "news-cache.json", overwrite=args.overwrite)
    _copy_optional(source_var / "scholar-cache.json", target_var / "scholar-cache.json", overwrite=args.overwrite)

    _validate_database(target_db)
    print(f"Copied local codex-lb state into {target_var}")
    print("Next step: run the fork's Alembic migrations against this copied DB before starting the fork service.")


def _copy_optional(source: Path, target: Path, *, overwrite: bool) -> None:
    if source.exists():
        _copy_file(source, target, overwrite=overwrite)


def _copy_file(source: Path, target: Path, *, overwrite: bool) -> None:
    if target.exists():
        if not overwrite:
            raise SystemExit(f"Target already exists: {target}. Pass --overwrite or choose another target.")
        backup = target.with_name(f"{target.name}.backup-{_timestamp()}")
        shutil.copy2(target, backup)
    shutil.copy2(source, target)


def _validate_database(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        revisions = [row[0] for row in conn.execute("SELECT version_num FROM alembic_version").fetchall()]
        if not revisions:
            raise SystemExit("Copied DB has no alembic_version row.")
        if LOCAL_PROVIDER_REVISION not in revisions:
            print(f"Warning: copied DB revisions are {revisions}; expected {LOCAL_PROVIDER_REVISION}.")

        columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()}
        missing = sorted(REQUIRED_ACCOUNT_COLUMNS - columns)
        if missing:
            raise SystemExit(f"Copied DB is missing provider-account columns: {', '.join(missing)}")


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


if __name__ == "__main__":
    main()
