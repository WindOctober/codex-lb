from __future__ import annotations

import logging
import os
import re

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.core.crypto import TokenEncryptor
from app.db.models import ApiKey
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

DEFAULT_REFRESH_API_KEY_NAME = "Pro-Only (Spread)"


def _normalize_key_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


async def load_codex_lb_refresh_api_key(
    *,
    value_env_keys: tuple[str, ...],
    name_env_keys: tuple[str, ...],
) -> str | None:
    for env_key in (*value_env_keys, "CODEX_LB_API_KEY"):
        value = os.getenv(env_key)
        if value:
            return value

    names = [os.getenv(env_key) for env_key in (*name_env_keys, "CODEX_LB_REFRESH_API_KEY_NAME")]
    names.append(DEFAULT_REFRESH_API_KEY_NAME)
    for name in names:
        if not name:
            continue
        stored_key = await _load_stored_api_key_by_name(name)
        if stored_key:
            return stored_key

    return None


async def _load_stored_api_key_by_name(name: str) -> str | None:
    wanted = _normalize_key_name(name)
    try:
        async with SessionLocal() as session:
            result = await session.execute(select(ApiKey).where(ApiKey.is_active.is_(True)).order_by(ApiKey.name))
            rows = list(result.scalars().all())
    except SQLAlchemyError:
        logger.exception("Failed to load stored refresh API key %s", name)
        return None

    for row in rows:
        if _normalize_key_name(row.name) != wanted:
            continue
        encrypted = getattr(row, "key_encrypted", None)
        if not encrypted:
            logger.warning("Refresh API key %s has no stored plaintext copy", row.name)
            return None
        try:
            return TokenEncryptor().decrypt(encrypted)
        except Exception:
            logger.exception("Failed to decrypt refresh API key %s", row.name)
            return None
    return None
