from __future__ import annotations

from app.db.models import ACCOUNT_PROVIDER_API_KEY, Account
from app.modules.proxy.helpers import _header_account_id


def _account_upstream_base_url(account: Account) -> str | None:
    if account.provider_kind != ACCOUNT_PROVIDER_API_KEY:
        return None
    return account.upstream_base_url


def _account_upstream_wire_api(account: Account) -> str:
    if account.provider_kind != ACCOUNT_PROVIDER_API_KEY:
        return "codex"
    return account.upstream_wire_api or "codex"


def _upstream_account_header_value(account: Account) -> str | None:
    if account.provider_kind == ACCOUNT_PROVIDER_API_KEY:
        return None
    return _header_account_id(account.chatgpt_account_id)


def _websocket_disabled_for_account(account: Account) -> bool:
    return account.provider_kind == ACCOUNT_PROVIDER_API_KEY and _account_upstream_wire_api(account) == "codex"
