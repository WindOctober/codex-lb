from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Iterable, Literal

from app.core.balancer.types import FailureClass, UpstreamError
from app.core.usage import PLAN_CAPACITY_CREDITS_SECONDARY
from app.core.utils.retry import backoff_seconds, parse_retry_after
from app.db.models import AccountStatus

PERMANENT_FAILURE_CODES = {
    "refresh_token_expired": "Refresh token expired - re-login required",
    "refresh_token_reused": "Refresh token was reused - re-login required",
    "refresh_token_invalidated": "Refresh token was revoked - re-login required",
    "account_deactivated": "Account has been deactivated",
    "account_suspended": "Account has been suspended",
    "account_deleted": "Account has been deleted",
}

SECONDS_PER_DAY = 60 * 60 * 24
UNKNOWN_RESET_BUCKET_DAYS = 10_000
RoutingStrategy = Literal["usage_weighted", "capacity_weighted", "high_waterline", "primary_drain"]
DEFAULT_ROUTING_STRATEGY: RoutingStrategy = "high_waterline"
UNKNOWN_PLAN_FALLBACK = "free"
CAPACITY_PLAN_ALIASES = {
    "education": "edu",
    "k12": "edu",
    "guest": "free",
    "go": "free",
    "free_workspace": "free",
    "quorum": "free",
    "unknown": "free",
}

HEALTH_TIER_HEALTHY = 0
HEALTH_TIER_DRAINING = 1
HEALTH_TIER_PROBING = 2

DRAIN_PRIMARY_THRESHOLD_PCT = 85.0
DRAIN_SECONDARY_THRESHOLD_PCT = 90.0
DRAIN_ERROR_WINDOW_SECONDS = 60.0
DRAIN_ERROR_COUNT_THRESHOLD = 2
PROBE_QUIET_SECONDS = 60.0
PROBE_SUCCESS_STREAK_REQUIRED = 3
RESET_PRIMER_MAX_USED_PERCENT = 0.0
RESET_PRIMER_MIN_SECONDS_UNTIL_SECONDARY_RESET = SECONDS_PER_DAY
RESET_PRIMER_SELECTION_COOLDOWN_SECONDS = 15 * 60.0


@dataclass
class AccountState:
    account_id: str
    status: AccountStatus
    used_percent: float | None = None
    reset_at: float | None = None
    blocked_at: float | None = None
    cooldown_until: float | None = None
    secondary_used_percent: float | None = None
    secondary_reset_at: int | None = None
    last_error_at: float | None = None
    last_selected_at: float | None = None
    error_count: int = 0
    deactivation_reason: str | None = None
    plan_type: str | None = None
    capacity_credits: float | None = None
    group_priority_rank: int = 1000000
    source_rank: int = 0
    configured_priority: int = 100
    health_tier: int = 0
    primary_drain_score: float = 0.0
    primary_drain_priority_enabled: bool = False


@dataclass
class SelectionResult:
    account: AccountState | None
    error_message: str | None
    retry_after_seconds: float | None = None


def _usage_sort_key(state: AccountState) -> tuple[int, float, float, float, float, str]:
    primary_used = state.used_percent if state.used_percent is not None else 0.0
    secondary_used = state.secondary_used_percent if state.secondary_used_percent is not None else primary_used
    last_selected = state.last_selected_at or 0.0
    return (
        state.group_priority_rank,
        float(state.source_rank),
        secondary_used,
        primary_used,
        last_selected,
        state.account_id,
    )


def _reset_bucket_days(state: AccountState, current: float) -> int:
    if state.secondary_reset_at is None:
        return UNKNOWN_RESET_BUCKET_DAYS
    return max(0, int((state.secondary_reset_at - current) // SECONDS_PER_DAY))


def _prefer_earlier_reset_candidates(available: list[AccountState], current: float) -> list[AccountState]:
    earliest_bucket = min(_reset_bucket_days(state, current) for state in available)
    return [state for state in available if _reset_bucket_days(state, current) == earliest_bucket]


def _fallback_secondary_capacity_credits(plan_type: str | None) -> float:
    normalized = (plan_type or "").strip().lower()
    resolved_plan = CAPACITY_PLAN_ALIASES.get(normalized, normalized or UNKNOWN_PLAN_FALLBACK)
    return PLAN_CAPACITY_CREDITS_SECONDARY.get(
        resolved_plan,
        PLAN_CAPACITY_CREDITS_SECONDARY[UNKNOWN_PLAN_FALLBACK],
    )


def select_account(
    states: Iterable[AccountState],
    now: float | None = None,
    *,
    prefer_earlier_reset: bool = False,
    routing_strategy: RoutingStrategy = DEFAULT_ROUTING_STRATEGY,
    allow_backoff_fallback: bool = True,
    deterministic_probe: bool = False,
) -> SelectionResult:
    """Select an eligible account by applying availability checks and routing strategy.

    This function filters out accounts that cannot currently serve traffic
    (for example paused, deactivated, still rate-limited, or in active
    cooldown), attempts controlled recovery from transient error backoff,
    and then chooses a candidate using the configured balancing strategy.

    Args:
        states: Candidate account states to evaluate for the current request.
        now: Unix timestamp in seconds used as the evaluation clock. If
            ``None``, the current system time is used.
        prefer_earlier_reset: Whether to bias selection toward accounts whose
            secondary quota window resets sooner.
        routing_strategy: Balancing strategy used to pick from the effective
            pool (``"primary_drain"``, ``"high_waterline"``,
            ``"capacity_weighted"``, or ``"usage_weighted"``).
            Eligible starred accounts are selected before the configured
            routing strategy runs. Primary-drain routes deterministically to
            accounts with a recent primary-window drain signal and falls back
            to capacity-weighted routing otherwise. High-waterline routes to a
            meaningfully underused account when one exists and also falls back
            to capacity-weighted routing otherwise.
        allow_backoff_fallback: Whether to allow a fallback attempt with the
            backoff account nearest to recovery when no fully available
            account exists.
        deterministic_probe: Whether capacity-weighted routing should use a
            deterministic probe order instead of random weighted choice.

    Returns:
        A ``SelectionResult`` containing the selected ``AccountState`` and no
        error message when routing can proceed, or ``None`` plus a
        human-readable error message when no account is eligible.
    """
    current = now or time.time()
    available: list[AccountState] = []
    in_error_backoff: list[AccountState] = []
    all_states = list(states)

    for state in all_states:
        if state.status == AccountStatus.DEACTIVATED:
            continue
        if state.status == AccountStatus.PAUSED:
            continue
        if state.status == AccountStatus.RATE_LIMITED:
            if state.reset_at and current >= state.reset_at:
                state.status = AccountStatus.ACTIVE
                state.error_count = 0
                state.reset_at = None
            else:
                continue
        if state.status == AccountStatus.QUOTA_EXCEEDED:
            if state.reset_at and current >= state.reset_at:
                state.status = AccountStatus.ACTIVE
                state.used_percent = 0.0
                state.reset_at = None
            else:
                continue
        if state.cooldown_until and current >= state.cooldown_until:
            state.cooldown_until = None
            state.last_error_at = None
            state.error_count = 0
        if state.cooldown_until and current < state.cooldown_until:
            continue
        if state.error_count >= 3:
            backoff = min(300, 30 * (2 ** (state.error_count - 3)))
            if state.last_error_at and current - state.last_error_at < backoff:
                in_error_backoff.append(state)
                continue
            # Error backoff expired — reset error state so recovery is
            # not penalised by stale counts. The account has already
            # been held back for the full backoff period; letting it
            # re-enter the pool with a clean slate avoids the problem
            # where a previously-high error_count causes an immediate
            # return to maximum backoff on the very next transient error.
            state.error_count = 0
            state.last_error_at = None
        available.append(state)

    if not available:
        hard_blocked_exists = any(
            state.status
            in (
                AccountStatus.PAUSED,
                AccountStatus.DEACTIVATED,
                AccountStatus.RATE_LIMITED,
                AccountStatus.QUOTA_EXCEEDED,
            )
            for state in all_states
        )
        if allow_backoff_fallback and (len(in_error_backoff) > 1 or (in_error_backoff and hard_blocked_exists)):

            def _backoff_expires_at(s: AccountState) -> float:
                backoff = min(300, 30 * (2 ** (s.error_count - 3)))
                return (s.last_error_at or 0.0) + backoff

            available.append(min(in_error_backoff, key=_backoff_expires_at))
        else:
            deactivated = [s for s in all_states if s.status == AccountStatus.DEACTIVATED]
            paused = [s for s in all_states if s.status == AccountStatus.PAUSED]
            rate_limited = [s for s in all_states if s.status == AccountStatus.RATE_LIMITED]
            quota_exceeded = [s for s in all_states if s.status == AccountStatus.QUOTA_EXCEEDED]

            if paused and deactivated and not rate_limited and not quota_exceeded:
                return SelectionResult(None, "All accounts are paused or require re-authentication")
            if paused and not rate_limited and not quota_exceeded:
                return SelectionResult(None, "All accounts are paused")
            if deactivated and not rate_limited and not quota_exceeded:
                return SelectionResult(None, "All accounts require re-authentication")
            reset_candidates = [s.reset_at for s in rate_limited if s.reset_at]
            reset_candidates.extend(s.reset_at for s in quota_exceeded if s.reset_at)
            cooldowns = [s.cooldown_until for s in all_states if s.cooldown_until and s.cooldown_until > current]
            recoverable_at = [float(value) for value in reset_candidates] + cooldowns
            if recoverable_at:
                wait_seconds = max(0.0, min(recoverable_at) - current)
                return SelectionResult(
                    None,
                    f"Rate limit exceeded. Try again in {wait_seconds:.0f}s",
                    retry_after_seconds=wait_seconds,
                )
            return SelectionResult(None, "No available accounts")

    def _reset_first_sort_key(state: AccountState) -> tuple[int, float, int, float, float, float, str]:
        reset_bucket_days = _reset_bucket_days(state, current)
        group_rank, source_rank, secondary_used, primary_used, last_selected, account_id = _usage_sort_key(state)
        return group_rank, source_rank, reset_bucket_days, secondary_used, primary_used, last_selected, account_id

    healthy = [s for s in available if s.health_tier == HEALTH_TIER_HEALTHY]
    probing = [s for s in available if s.health_tier == HEALTH_TIER_PROBING]
    draining = [s for s in available if s.health_tier == HEALTH_TIER_DRAINING]
    effective_pool = healthy or probing or draining or available
    starred_account = _select_starred_account(available)
    if starred_account is not None:
        return SelectionResult(starred_account, None)
    reset_primer = _select_reset_primer_account(effective_pool, current)
    if reset_primer is not None:
        return SelectionResult(reset_primer, None)

    if routing_strategy == "capacity_weighted":
        selected = _select_capacity_weighted_from_pool(
            effective_pool,
            current=current,
            prefer_earlier_reset=prefer_earlier_reset,
            deterministic_probe=deterministic_probe,
        )
    elif routing_strategy == "primary_drain":
        selected = _select_primary_drain_account(effective_pool)
        if selected is None:
            selected = _select_capacity_weighted_from_pool(
                effective_pool,
                current=current,
                prefer_earlier_reset=prefer_earlier_reset,
                deterministic_probe=deterministic_probe,
            )
    elif routing_strategy == "high_waterline":
        selected = _select_high_waterline_account(effective_pool)
        if selected is None:
            selected = _select_capacity_weighted_from_pool(
                effective_pool,
                current=current,
                prefer_earlier_reset=prefer_earlier_reset,
                deterministic_probe=deterministic_probe,
            )
    else:
        selected = min(effective_pool, key=_reset_first_sort_key if prefer_earlier_reset else _usage_sort_key)
    return SelectionResult(selected, None)


def _min_group_source_candidates(available: list[AccountState]) -> list[AccountState]:
    min_group_rank = min(state.group_priority_rank for state in available)
    candidate_pool = [state for state in available if state.group_priority_rank == min_group_rank]
    min_source_rank = min(state.source_rank for state in candidate_pool)
    return [state for state in candidate_pool if state.source_rank == min_source_rank]


def _select_capacity_weighted_from_pool(
    available: list[AccountState],
    *,
    current: float,
    prefer_earlier_reset: bool,
    deterministic_probe: bool,
) -> AccountState:
    candidate_pool = _prefer_earlier_reset_candidates(available, current) if prefer_earlier_reset else available
    if not candidate_pool:
        candidate_pool = available
    candidate_pool = _min_group_source_candidates(candidate_pool)
    if deterministic_probe:
        return min(candidate_pool, key=_capacity_probe_sort_key)
    return _select_capacity_weighted(candidate_pool)


def _remaining_secondary_credits(state: AccountState) -> float:
    """Return remaining absolute credits for the secondary (7-day) window."""
    capacity = state.capacity_credits
    if capacity is None:
        capacity = _fallback_secondary_capacity_credits(state.plan_type)
    elif capacity <= 0:
        return 0.0
    if state.secondary_used_percent is not None:
        used_pct = state.secondary_used_percent
    elif state.used_percent is not None:
        used_pct = state.used_percent
    else:
        used_pct = 0.0
    return max(0.0, capacity * (1.0 - min(used_pct, 100.0) / 100.0))


def _configured_priority_weight_factor(state: AccountState) -> float:
    configured_priority = state.configured_priority if state.configured_priority > 0 else 1
    return 100.0 / float(configured_priority)


def _capacity_selection_weight(state: AccountState) -> float:
    return _remaining_secondary_credits(state) * _configured_priority_weight_factor(state)


HIGH_WATERLINE_MARGIN_PCT = 1.0
PRIMARY_DRAIN_MIN_SCORE = 1.0


def _remaining_percent_for_waterline(state: AccountState) -> float:
    if state.secondary_used_percent is not None:
        used_percent = state.secondary_used_percent
    elif state.used_percent is not None:
        used_percent = state.used_percent
    else:
        used_percent = 0.0
    return max(0.0, min(100.0, 100.0 - float(used_percent)))


def _select_high_waterline_account(available: list[AccountState]) -> AccountState | None:
    candidate_pool = _min_group_source_candidates(available)
    if len(candidate_pool) < 2:
        return None

    average_remaining = sum(_remaining_percent_for_waterline(state) for state in candidate_pool) / len(candidate_pool)
    selected = max(
        candidate_pool,
        key=lambda state: (
            _remaining_percent_for_waterline(state),
            -float(state.configured_priority if state.configured_priority > 0 else 1),
            -(state.last_selected_at or 0.0),
            state.account_id,
        ),
    )
    if _remaining_percent_for_waterline(selected) < average_remaining + HIGH_WATERLINE_MARGIN_PCT:
        return None
    return selected


def _select_starred_account(available: list[AccountState]) -> AccountState | None:
    priority_pool = [state for state in available if state.primary_drain_priority_enabled]
    if not priority_pool:
        return None
    priority_full_primary_pool = [
        state
        for state in priority_pool
        if state.used_percent is not None and state.used_percent >= 100.0
    ]
    candidate_pool = _min_group_source_candidates(priority_full_primary_pool or priority_pool)
    return min(
        candidate_pool,
        key=lambda state: (
            -float(state.primary_drain_score),
            -(state.used_percent if state.used_percent is not None else 0.0),
            state.configured_priority if state.configured_priority > 0 else 1,
            state.last_selected_at or 0.0,
            state.account_id,
        ),
    )


def _select_primary_drain_account(available: list[AccountState]) -> AccountState | None:
    candidate_pool = _min_group_source_candidates(available)
    selected = min(
        candidate_pool,
        key=lambda state: (
            -float(state.primary_drain_score),
            -(state.used_percent if state.used_percent is not None else 0.0),
            state.configured_priority if state.configured_priority > 0 else 1,
            state.last_selected_at or 0.0,
            state.account_id,
        ),
    )
    if selected.primary_drain_score < PRIMARY_DRAIN_MIN_SCORE:
        return None
    return selected


def _select_reset_primer_account(available: list[AccountState], current: float) -> AccountState | None:
    primer_candidates = [state for state in available if _is_reset_primer_candidate(state, current)]
    if not primer_candidates:
        return None
    candidate_pool = _min_group_source_candidates(primer_candidates)
    return min(
        candidate_pool,
        key=lambda state: (
            state.last_selected_at or 0.0,
            state.configured_priority if state.configured_priority > 0 else 1,
            state.secondary_reset_at or float("inf"),
            state.account_id,
        ),
    )


def _is_reset_primer_candidate(state: AccountState, current: float) -> bool:
    if state.secondary_used_percent is None or state.secondary_reset_at is None:
        return False
    if float(state.secondary_used_percent) > RESET_PRIMER_MAX_USED_PERCENT:
        return False
    if state.secondary_reset_at - current < RESET_PRIMER_MIN_SECONDS_UNTIL_SECONDARY_RESET:
        return False
    if (
        state.last_selected_at is not None
        and current - state.last_selected_at < RESET_PRIMER_SELECTION_COOLDOWN_SECONDS
    ):
        return False
    return True


def _capacity_probe_sort_key(state: AccountState) -> tuple[int, float, float, float, float, float, str]:
    group_rank, source_rank, secondary_used, primary_used, last_selected, account_id = _usage_sort_key(state)
    return (
        group_rank,
        source_rank,
        -_capacity_selection_weight(state),
        secondary_used,
        primary_used,
        last_selected,
        account_id,
    )


def _select_capacity_weighted(available: list[AccountState]) -> AccountState:
    """Select an account with probability proportional to priority-adjusted remaining credits."""
    weights = [_capacity_selection_weight(s) for s in available]
    total = sum(weights)
    if total <= 0.0:
        # All accounts exhausted — fall back to deterministic usage-weighted
        return min(available, key=_usage_sort_key)
    return random.choices(available, weights=weights, k=1)[0]


def handle_rate_limit(state: AccountState, error: UpstreamError) -> None:
    state.status = AccountStatus.RATE_LIMITED
    state.error_count += 1
    state.last_error_at = time.time()
    state.blocked_at = time.time()

    reset_at = _extract_reset_at(error)
    if reset_at is not None:
        state.reset_at = reset_at

    message = error.get("message")
    delay = parse_retry_after(message) if message else None
    if delay is None:
        delay = backoff_seconds(state.error_count)
    state.cooldown_until = time.time() + delay


QUOTA_EXCEEDED_COOLDOWN_SECONDS = 120.0


def handle_quota_exceeded(state: AccountState, error: UpstreamError) -> None:
    state.status = AccountStatus.QUOTA_EXCEEDED
    state.used_percent = 100.0
    state.blocked_at = time.time()
    state.cooldown_until = time.time() + QUOTA_EXCEEDED_COOLDOWN_SECONDS

    reset_at = _extract_reset_at(error)
    if reset_at is not None:
        state.reset_at = reset_at
    else:
        state.reset_at = int(time.time() + 3600)


def handle_permanent_failure(state: AccountState, error_code: str) -> None:
    state.status = AccountStatus.DEACTIVATED
    state.deactivation_reason = PERMANENT_FAILURE_CODES.get(
        error_code,
        f"Authentication failed: {error_code}",
    )
    state.blocked_at = None


FailoverAction = Literal["failover_next", "surface"]


def failover_decision(
    *,
    failure_class: FailureClass,
    downstream_visible: bool,
    candidates_remaining: int,
) -> FailoverAction:
    if downstream_visible:
        return "surface"
    if candidates_remaining <= 0:
        return "surface"
    if failure_class in ("rate_limit", "quota", "retryable_transient"):
        return "failover_next"
    return "surface"


def _extract_reset_at(error: UpstreamError) -> int | None:
    reset_at = error.get("resets_at")
    if reset_at is not None:
        return int(reset_at)
    reset_in = error.get("resets_in_seconds")
    if reset_in is not None:
        return int(time.time() + float(reset_in))
    return None


def evaluate_health_tier(
    state: AccountState,
    *,
    now: float | None = None,
    drain_entered_at: float | None = None,
    probe_success_streak: int = 0,
    drain_primary_threshold_pct: float = DRAIN_PRIMARY_THRESHOLD_PCT,
    drain_secondary_threshold_pct: float = DRAIN_SECONDARY_THRESHOLD_PCT,
    drain_error_window_seconds: float = DRAIN_ERROR_WINDOW_SECONDS,
    drain_error_count_threshold: int = DRAIN_ERROR_COUNT_THRESHOLD,
    probe_quiet_seconds: float = PROBE_QUIET_SECONDS,
    probe_success_streak_required: int = PROBE_SUCCESS_STREAK_REQUIRED,
) -> int:
    current = now or time.time()

    if state.status in (
        AccountStatus.RATE_LIMITED,
        AccountStatus.QUOTA_EXCEEDED,
        AccountStatus.PAUSED,
        AccountStatus.DEACTIVATED,
    ):
        return state.health_tier

    should_drain = False

    if state.used_percent is not None and state.used_percent >= drain_primary_threshold_pct:
        should_drain = True

    if state.secondary_used_percent is not None and state.secondary_used_percent >= drain_secondary_threshold_pct:
        should_drain = True

    if (
        state.error_count >= drain_error_count_threshold
        and state.last_error_at is not None
        and current - state.last_error_at < drain_error_window_seconds
    ):
        should_drain = True

    current_tier = state.health_tier

    if current_tier == HEALTH_TIER_HEALTHY:
        return HEALTH_TIER_DRAINING if should_drain else HEALTH_TIER_HEALTHY

    if current_tier == HEALTH_TIER_DRAINING:
        if should_drain:
            return HEALTH_TIER_DRAINING
        if drain_entered_at is not None and current - drain_entered_at >= probe_quiet_seconds:
            return HEALTH_TIER_PROBING
        return HEALTH_TIER_DRAINING

    if current_tier == HEALTH_TIER_PROBING:
        if should_drain:
            return HEALTH_TIER_DRAINING
        if probe_success_streak >= probe_success_streak_required:
            return HEALTH_TIER_HEALTHY
        return HEALTH_TIER_PROBING

    return HEALTH_TIER_HEALTHY
