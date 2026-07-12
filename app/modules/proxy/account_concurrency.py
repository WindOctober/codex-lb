from __future__ import annotations

import logging
from dataclasses import dataclass
from threading import Lock

logger = logging.getLogger(__name__)

_UNKNOWN_MODEL_KEY = ""


@dataclass(slots=True)
class AccountModelConcurrencyLease:
    _limiter: AccountModelConcurrencyLimiter | None
    account_id: str
    model_key: str
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._limiter is not None:
            self._limiter._release(self.account_id, self.model_key)

    def __enter__(self) -> AccountModelConcurrencyLease:
        return self

    def __exit__(self, *args: object) -> None:
        self.release()

    def __del__(self) -> None:
        if self._released:
            return
        self._released = True
        if self._limiter is not None:
            self._limiter._release(self.account_id, self.model_key)
            logger.warning(
                "AccountModelConcurrencyLease was garbage-collected without release() account_id=%s model=%s",
                self.account_id,
                self.model_key or "<unknown>",
            )


class AccountModelConcurrencyLimiter:
    def __init__(self) -> None:
        self._lock = Lock()
        self._active: dict[tuple[str, str], int] = {}

    def try_acquire(
        self,
        *,
        account_id: str,
        model: str | None,
        limit: int,
    ) -> AccountModelConcurrencyLease | None:
        model_key = _model_key(model)
        if limit <= 0:
            return AccountModelConcurrencyLease(None, account_id, model_key)
        with self._lock:
            key = (account_id, model_key)
            active = self._active.get(key, 0)
            if active >= limit:
                return None
            self._active[key] = active + 1
        return AccountModelConcurrencyLease(self, account_id, model_key)

    def full_account_ids(self, *, model: str | None, limit: int) -> set[str]:
        if limit <= 0:
            return set()
        model_key = _model_key(model)
        with self._lock:
            return {
                account_id
                for (account_id, active_model_key), active in self._active.items()
                if active_model_key == model_key and active >= limit
            }

    def active_count(self, *, account_id: str, model: str | None) -> int:
        with self._lock:
            return self._active.get((account_id, _model_key(model)), 0)

    def _release(self, account_id: str, model_key: str) -> None:
        with self._lock:
            key = (account_id, model_key)
            active = self._active.get(key, 0)
            if active <= 1:
                self._active.pop(key, None)
            else:
                self._active[key] = active - 1


def _model_key(model: str | None) -> str:
    if model is None:
        return _UNKNOWN_MODEL_KEY
    return model.strip() or _UNKNOWN_MODEL_KEY
