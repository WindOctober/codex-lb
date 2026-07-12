from __future__ import annotations

import argparse
import asyncio
import json
import os


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge one codex-lb account into another.")
    parser.add_argument("source_account_id", help="Existing account ID to move data from and delete.")
    parser.add_argument("target_account_id", help="Existing account ID to move data into.")
    parser.add_argument(
        "--db-url",
        default=None,
        help="Database URL. Defaults to CODEX_LB_DATABASE_URL from settings.",
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    if args.db_url:
        os.environ["CODEX_LB_DATABASE_URL"] = args.db_url

    from app.core.cache.invalidation import NAMESPACE_API_KEY, CacheInvalidationPoller
    from app.db.session import SessionLocal, close_db
    from app.modules.accounts.repository import AccountsRepository
    from app.modules.accounts.service import AccountMergeValidationError, AccountsService

    try:
        async with SessionLocal() as session:
            service = AccountsService(AccountsRepository(session))
            response = await service.merge_accounts(args.source_account_id, args.target_account_id)

        poller = CacheInvalidationPoller(SessionLocal)
        await poller.bump(NAMESPACE_API_KEY)
        print(json.dumps(response.model_dump(mode="json", by_alias=True), indent=2, sort_keys=True))
        return 0
    except AccountMergeValidationError as exc:
        print(f"account_merge_error={exc}")
        return 2
    finally:
        await close_db()


def main() -> None:
    raise SystemExit(asyncio.run(_run(_parse_args())))


if __name__ == "__main__":
    main()
