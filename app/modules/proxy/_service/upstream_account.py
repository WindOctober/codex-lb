from __future__ import annotations

from app.db.models import ACCOUNT_PROVIDER_API_KEY, ACCOUNT_PROVIDER_OPENAI_OAUTH, Account
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


def _account_supports_required_upstream_wire_api(
    account: Account,
    required_wire_api: str | None,
) -> bool:
    if required_wire_api is None:
        return True
    if required_wire_api == "responses":
        return account.provider_kind == ACCOUNT_PROVIDER_API_KEY and _account_upstream_wire_api(account) in {
            "responses",
            "v1",
        }
    if required_wire_api == "codex":
        return account.provider_kind == ACCOUNT_PROVIDER_OPENAI_OAUTH or (
            account.provider_kind == ACCOUNT_PROVIDER_API_KEY and _account_upstream_wire_api(account) == "codex"
        )
    return (
        account.provider_kind == ACCOUNT_PROVIDER_API_KEY and _account_upstream_wire_api(account) == required_wire_api
    )
