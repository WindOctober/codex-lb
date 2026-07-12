from __future__ import annotations

from fastapi import APIRouter, Body, Depends, File, Request, UploadFile

from app.core.audit.service import AuditService
from app.core.auth.dependencies import set_dashboard_error_format, validate_dashboard_session
from app.core.exceptions import DashboardBadRequestError, DashboardConflictError, DashboardNotFoundError
from app.dependencies import AccountsContext, ProxyContext, get_accounts_context, get_proxy_context
from app.modules.accounts.repository import AccountIdentityConflictError
from app.modules.accounts.schemas import (
    AccountAvailabilityResponse,
    AccountDeleteResponse,
    AccountFastServiceTierBulkUpdateRequest,
    AccountFastServiceTierBulkUpdateResponse,
    AccountImportResponse,
    AccountMergeRequest,
    AccountMergeResponse,
    AccountPauseResponse,
    AccountQuotaListResponse,
    AccountQuotaResponse,
    AccountRateLimitResetConsumeRequest,
    AccountRateLimitResetConsumeResponse,
    AccountRateLimitResetCreditsResponse,
    AccountReactivateResponse,
    AccountsResponse,
    AccountSummary,
    AccountTrendsResponse,
    AccountUpdateRequest,
    ApiProviderCreateRequest,
    ApiProviderCreateResponse,
)
from app.modules.accounts.service import (
    AccountMergeValidationError,
    AccountResetCreditError,
    AccountRuntimeOccupancy,
    InvalidApiProviderError,
    InvalidAuthJsonError,
)

router = APIRouter(
    prefix="/api/accounts",
    tags=["dashboard"],
    dependencies=[Depends(validate_dashboard_session), Depends(set_dashboard_error_format)],
)


@router.get("", response_model=AccountsResponse)
async def list_accounts(
    context: AccountsContext = Depends(get_accounts_context),
) -> AccountsResponse:
    accounts = await context.service.list_accounts()
    return AccountsResponse(accounts=accounts)


@router.get("/quota", response_model=AccountQuotaListResponse)
async def list_account_quota_statuses(
    context: AccountsContext = Depends(get_accounts_context),
    proxy_context: ProxyContext = Depends(get_proxy_context),
) -> AccountQuotaListResponse:
    runtime_occupancy = await _load_account_runtime_occupancy(proxy_context)
    accounts = await context.service.list_account_quota_statuses(runtime_occupancy=runtime_occupancy)
    return AccountQuotaListResponse(accounts=accounts)


@router.get("/available", response_model=AccountQuotaListResponse)
async def list_available_account_candidates(
    context: AccountsContext = Depends(get_accounts_context),
    proxy_context: ProxyContext = Depends(get_proxy_context),
) -> AccountQuotaListResponse:
    return await list_account_quota_statuses(context=context, proxy_context=proxy_context)


@router.get("/{account_id}/quota", response_model=AccountQuotaResponse)
async def get_account_quota(
    account_id: str,
    context: AccountsContext = Depends(get_accounts_context),
    proxy_context: ProxyContext = Depends(get_proxy_context),
) -> AccountQuotaResponse:
    runtime_occupancy = await _load_account_runtime_occupancy(proxy_context)
    account = await context.service.get_account_quota_status(account_id, runtime_occupancy=runtime_occupancy)
    if not account:
        raise DashboardNotFoundError("Account not found", code="account_not_found")
    return AccountQuotaResponse(account=account)


@router.get("/{account_id}/trends", response_model=AccountTrendsResponse)
async def get_account_trends(
    account_id: str,
    context: AccountsContext = Depends(get_accounts_context),
) -> AccountTrendsResponse:
    result = await context.service.get_account_trends(account_id)
    if not result:
        raise DashboardNotFoundError("Account not found", code="account_not_found")
    return result


@router.get("/{account_id}/rate-limit-reset-credits", response_model=AccountRateLimitResetCreditsResponse)
async def get_account_rate_limit_reset_credits(
    account_id: str,
    context: AccountsContext = Depends(get_accounts_context),
) -> AccountRateLimitResetCreditsResponse:
    try:
        result = await context.service.get_rate_limit_reset_credits(account_id)
    except AccountResetCreditError as exc:
        raise DashboardBadRequestError(str(exc), code="account_reset_credit_unavailable") from exc
    if not result:
        raise DashboardNotFoundError("Account not found", code="account_not_found")
    return result


@router.post("/{account_id}/rate-limit-reset-credits/consume", response_model=AccountRateLimitResetConsumeResponse)
async def consume_account_rate_limit_reset_credit(
    request: Request,
    account_id: str,
    payload: AccountRateLimitResetConsumeRequest | None = Body(default=None),
    context: AccountsContext = Depends(get_accounts_context),
) -> AccountRateLimitResetConsumeResponse:
    try:
        response = await context.service.consume_rate_limit_reset_credit(
            account_id,
            idempotency_key=payload.idempotency_key if payload else None,
        )
    except AccountResetCreditError as exc:
        raise DashboardBadRequestError(str(exc), code="account_reset_credit_unavailable") from exc
    if response is None:
        raise DashboardNotFoundError("Account not found", code="account_not_found")
    AuditService.log_async(
        "account_rate_limit_reset_credit_consumed",
        actor_ip=request.client.host if request.client else None,
        details={
            "account_id": response.account_id,
            "outcome": response.outcome,
            "windows_reset": response.windows_reset,
        },
    )
    return response


@router.post("/{account_id}/availability", response_model=AccountAvailabilityResponse)
async def test_account_availability(
    account_id: str,
    context: AccountsContext = Depends(get_accounts_context),
) -> AccountAvailabilityResponse:
    result = await context.service.test_availability(account_id)
    if not result:
        raise DashboardNotFoundError("Account not found", code="account_not_found")
    return result


@router.post("/import", response_model=AccountImportResponse)
async def import_account(
    request: Request,
    auth_json: UploadFile = File(...),
    context: AccountsContext = Depends(get_accounts_context),
) -> AccountImportResponse:
    raw = await auth_json.read()
    try:
        response = await context.service.import_account(raw)
        AuditService.log_async(
            "account_created",
            actor_ip=request.client.host if request.client else None,
            details={"account_id": response.account_id},
        )
        return response
    except InvalidAuthJsonError as exc:
        raise DashboardBadRequestError("Invalid auth.json payload", code="invalid_auth_json") from exc
    except AccountIdentityConflictError as exc:
        raise DashboardConflictError(str(exc), code="duplicate_identity_conflict") from exc


@router.post("/providers", response_model=ApiProviderCreateResponse)
async def create_api_provider(
    request: Request,
    payload: ApiProviderCreateRequest = Body(...),
    context: AccountsContext = Depends(get_accounts_context),
) -> ApiProviderCreateResponse:
    try:
        response = await context.service.create_api_provider(payload)
        AuditService.log_async(
            "api_provider_created",
            actor_ip=request.client.host if request.client else None,
            details={"account_id": response.account_id, "base_url": response.base_url},
        )
        return response
    except InvalidApiProviderError as exc:
        raise DashboardBadRequestError(str(exc), code="invalid_api_provider") from exc


@router.post("/merge", response_model=AccountMergeResponse)
async def merge_accounts(
    request: Request,
    payload: AccountMergeRequest = Body(...),
    context: AccountsContext = Depends(get_accounts_context),
) -> AccountMergeResponse:
    try:
        response = await context.service.merge_accounts(payload.source_account_id, payload.target_account_id)
    except AccountMergeValidationError as exc:
        raise DashboardBadRequestError(str(exc), code="invalid_account_merge") from exc
    AuditService.log_async(
        "accounts_merged",
        actor_ip=request.client.host if request.client else None,
        details={"source_account_id": response.source_account_id, "target_account_id": response.target_account_id},
    )
    return response


@router.post("/fast-service-tier", response_model=AccountFastServiceTierBulkUpdateResponse)
async def update_all_accounts_fast_service_tier(
    request: Request,
    payload: AccountFastServiceTierBulkUpdateRequest = Body(...),
    context: AccountsContext = Depends(get_accounts_context),
) -> AccountFastServiceTierBulkUpdateResponse:
    response = await context.service.update_all_fast_service_tier(enabled=payload.enabled)
    AuditService.log_async(
        "accounts_fast_service_tier_updated",
        actor_ip=request.client.host if request.client else None,
        details={"enabled": response.enabled, "updated_count": response.updated_count},
    )
    return response


@router.post("/{account_id}/reactivate", response_model=AccountReactivateResponse)
async def reactivate_account(
    account_id: str,
    context: AccountsContext = Depends(get_accounts_context),
) -> AccountReactivateResponse:
    success = await context.service.reactivate_account(account_id)
    if not success:
        raise DashboardNotFoundError("Account not found", code="account_not_found")
    return AccountReactivateResponse(status="reactivated")


@router.post("/{account_id}/pause", response_model=AccountPauseResponse)
async def pause_account(
    account_id: str,
    context: AccountsContext = Depends(get_accounts_context),
) -> AccountPauseResponse:
    success = await context.service.pause_account(account_id)
    if not success:
        raise DashboardNotFoundError("Account not found", code="account_not_found")
    return AccountPauseResponse(status="paused")


@router.patch("/{account_id}", response_model=AccountSummary)
async def update_account(
    account_id: str,
    payload: AccountUpdateRequest = Body(...),
    context: AccountsContext = Depends(get_accounts_context),
) -> AccountSummary:
    try:
        account = await context.service.update_account(account_id, payload)
    except ValueError as exc:
        raise DashboardBadRequestError(str(exc), code="invalid_account_payload") from exc
    if account is None:
        raise DashboardNotFoundError("Account not found", code="account_not_found")
    return account


@router.delete("/{account_id}", response_model=AccountDeleteResponse)
async def delete_account(
    request: Request,
    account_id: str,
    context: AccountsContext = Depends(get_accounts_context),
) -> AccountDeleteResponse:
    success = await context.service.delete_account(account_id)
    if not success:
        raise DashboardNotFoundError("Account not found", code="account_not_found")
    AuditService.log_async(
        "account_deleted",
        actor_ip=request.client.host if request.client else None,
        details={"account_id": account_id},
    )
    return AccountDeleteResponse(status="deleted")


async def _load_account_runtime_occupancy(proxy_context: ProxyContext) -> dict[str, AccountRuntimeOccupancy]:
    runtime_snapshot = await proxy_context.service.get_http_bridge_account_runtime_snapshot()
    return {
        account_id: AccountRuntimeOccupancy(
            sessions=snapshot.sessions,
            pending_requests=snapshot.pending_requests,
            queued_requests=snapshot.queued_requests,
            busy_sessions=snapshot.busy_sessions,
            codex_sessions=snapshot.codex_sessions,
            reconnect_requested_sessions=snapshot.reconnect_requested_sessions,
        )
        for account_id, snapshot in runtime_snapshot.items()
    }
