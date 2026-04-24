from __future__ import annotations

from app.db.models import ACCOUNT_PROVIDER_API_KEY, ACCOUNT_PROVIDER_OPENAI_OAUTH, Account


def account_configured_priority(account: Account) -> int:
    value = getattr(account, "upstream_priority", 100)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 100


def account_routing_tier(account: Account) -> str:
    if account.provider_kind == ACCOUNT_PROVIDER_API_KEY:
        return "api_key_provider"
    if account.provider_kind == ACCOUNT_PROVIDER_OPENAI_OAUTH:
        return "openai_paid"
    return account.provider_kind or "unknown"


def account_routing_priority(account: Account) -> int:
    if account.provider_kind == ACCOUNT_PROVIDER_API_KEY:
        return max(0, account_configured_priority(account))
    if account.provider_kind == ACCOUNT_PROVIDER_OPENAI_OAUTH:
        return 0
    return max(0, account_configured_priority(account))
