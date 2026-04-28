from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from sqlalchemy import select

from app.core.clients.upstream import probe_upstream_provider
from app.core.crypto import TokenEncryptor
from app.db.models import ACCOUNT_PROVIDER_API_KEY, Account, AccountStatus
from app.db.session import get_background_session
from app.modules.proxy.account_cache import get_account_selection_cache

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProviderModelRefreshResult:
    checked: int
    refreshed: int
    changed: int
    failed: int


async def refresh_api_provider_model_snapshots_on_startup() -> ProviderModelRefreshResult:
    """Refresh model lists advertised by API-key upstream providers.

    This is intentionally best-effort at the caller: individual provider probe
    failures keep the previous snapshot so startup can continue.
    """
    async with get_background_session() as session:
        result = await session.execute(
            select(Account)
            .where(Account.provider_kind == ACCOUNT_PROVIDER_API_KEY)
            .where(Account.status.notin_((AccountStatus.PAUSED, AccountStatus.DEACTIVATED)))
        )
        accounts = list(result.scalars().all())

        encryptor = TokenEncryptor()
        checked = refreshed = changed = failed = 0

        for account in accounts:
            checked += 1
            if not account.upstream_base_url:
                failed += 1
                logger.warning("Skipping API provider model refresh with missing base URL account=%s", account.id)
                continue

            try:
                api_key = encryptor.decrypt(account.access_token_encrypted)
                probe = await probe_upstream_provider(base_url=account.upstream_base_url, api_key=api_key)
            except Exception:
                failed += 1
                logger.warning("API provider model refresh failed account=%s", account.id, exc_info=True)
                continue

            refreshed += 1
            supported_models_json = (
                json.dumps(list(probe.supported_models), ensure_ascii=True) if probe.supported_models else None
            )
            if (
                account.upstream_base_url == probe.base_url
                and account.upstream_wire_api == probe.wire_api
                and account.supported_models_json == supported_models_json
            ):
                continue

            account.upstream_base_url = probe.base_url
            account.upstream_wire_api = probe.wire_api
            account.supported_models_json = supported_models_json
            changed += 1

        if changed:
            await session.commit()
            get_account_selection_cache().invalidate()

        return ProviderModelRefreshResult(
            checked=checked,
            refreshed=refreshed,
            changed=changed,
            failed=failed,
        )
