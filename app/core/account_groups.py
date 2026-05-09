from __future__ import annotations

import re
from typing import Any

from app.db.models import ACCOUNT_PROVIDER_API_KEY

GROUP_GENERAL = "general"
GROUP_KYC = "kyc"
GROUP_PLUS = "plus"
GROUP_PRO = "pro"
GROUP_PROVIDER_PREFIX = "provider:"

_PROVIDER_GROUP_SLUG_MAX_LEN = 80
_PROVIDER_GROUP_SAFE_CHARS = re.compile(r"[^a-z0-9._:-]+")


def account_builtin_group_names(account: Any) -> set[str]:
    if getattr(account, "provider_kind", None) == ACCOUNT_PROVIDER_API_KEY:
        return {_provider_group_name(account)}

    groups: set[str] = set()
    if bool(getattr(account, "kyc_enabled", False)):
        groups.add(GROUP_KYC)
    plan_type = str(getattr(account, "plan_type", "") or "").strip().lower()
    if plan_type == GROUP_PLUS:
        groups.add(GROUP_PLUS)
    if plan_type == GROUP_PRO:
        groups.add(GROUP_PRO)
    if not groups:
        groups.add(GROUP_GENERAL)
    return groups


def is_reserved_account_group(group_name: str) -> bool:
    normalized = group_name.strip().lower()
    return normalized in {GROUP_GENERAL, GROUP_KYC, GROUP_PLUS, GROUP_PRO} or normalized.startswith(
        GROUP_PROVIDER_PREFIX
    )


def _provider_group_name(account: Any) -> str:
    label = str(getattr(account, "email", "") or getattr(account, "id", "") or "provider")
    slug = _PROVIDER_GROUP_SAFE_CHARS.sub("-", label.strip().lower()).strip("-._:")
    if not slug:
        slug = "provider"
    slug = slug[:_PROVIDER_GROUP_SLUG_MAX_LEN].strip("-._:") or "provider"
    account_id = str(getattr(account, "id", "") or "")
    suffix = _PROVIDER_GROUP_SAFE_CHARS.sub("-", account_id.strip().lower()).strip("-._:")
    if suffix.startswith("provider-"):
        suffix = suffix.removeprefix("provider-")
    if suffix.startswith("provider_"):
        suffix = suffix.removeprefix("provider_")
    suffix = suffix[-12:] if suffix else "account"
    return f"{GROUP_PROVIDER_PREFIX}{slug}:{suffix}"
