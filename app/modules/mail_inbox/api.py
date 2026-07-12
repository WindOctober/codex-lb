from __future__ import annotations

import json

from fastapi import APIRouter, Body, Depends, Query

from app.core.auth.dependencies import set_dashboard_error_format, validate_dashboard_session
from app.core.exceptions import DashboardBadRequestError, DashboardNotFoundError
from app.db.models import MailAccount, MailFocusRule, MailMessage
from app.dependencies import MailInboxContext, get_mail_inbox_context
from app.modules.mail_inbox.schemas import (
    MailAccountCreateRequest,
    MailAccountResponse,
    MailAccountsResponse,
    MailAccountSyncResponse,
    MailAccountUpdateRequest,
    MailFocusRuleCreateRequest,
    MailFocusRuleResponse,
    MailFocusRulesResponse,
    MailFocusRuleUpdateRequest,
    MailMessageIngestRequest,
    MailMessageResponse,
    MailMessagesResponse,
)
from app.modules.mail_inbox.service import (
    MailAccountCreateData,
    MailInboxNotFoundError,
    MailInboxValidationError,
    MailMessageIngestData,
)

router = APIRouter(
    prefix="/api/mail",
    tags=["mail"],
    dependencies=[Depends(validate_dashboard_session), Depends(set_dashboard_error_format)],
)

def _account_response(account: MailAccount) -> MailAccountResponse:
    return MailAccountResponse(
        id=account.id,
        provider=account.provider.value,
        address=account.address,
        display_name=account.display_name,
        enabled=account.enabled,
        sync_status=account.sync_status.value,
        last_sync_at=account.last_sync_at,
        last_sync_error=account.last_sync_error,
        imap_host=account.imap_host,
        imap_port=account.imap_port,
        imap_username=account.imap_username,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


def _message_response(message: MailMessage) -> MailMessageResponse:
    return MailMessageResponse(
        id=message.id,
        account_id=message.account_id,
        account_address=message.account.address,
        account_provider=message.account.provider.value,
        provider_message_id=message.provider_message_id,
        thread_id=message.thread_id,
        sender_email=message.sender_email,
        sender_name=message.sender_name,
        recipients=json.loads(message.recipients_json),
        subject=message.subject,
        snippet=message.snippet,
        received_at=message.received_at,
        unread=message.unread,
        starred=message.starred,
        has_attachments=message.has_attachments,
        focused=message.focused,
        focus_label=message.focus_label,
    )


def _rule_response(rule: MailFocusRule) -> MailFocusRuleResponse:
    return MailFocusRuleResponse(
        id=rule.id,
        kind=rule.kind.value,
        value=rule.value,
        label=rule.label,
        enabled=rule.enabled,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


@router.get("/accounts", response_model=MailAccountsResponse)
async def list_mail_accounts(context: MailInboxContext = Depends(get_mail_inbox_context)) -> MailAccountsResponse:
    accounts = await context.service.list_accounts()
    return MailAccountsResponse(accounts=[_account_response(account) for account in accounts])


@router.post("/accounts", response_model=MailAccountResponse)
async def create_mail_account(
    payload: MailAccountCreateRequest = Body(...),
    context: MailInboxContext = Depends(get_mail_inbox_context),
) -> MailAccountResponse:
    try:
        account = await context.service.create_account(
            MailAccountCreateData(
                provider=payload.provider,
                address=payload.address,
                display_name=payload.display_name,
                imap_host=payload.imap_host,
                imap_port=payload.imap_port,
                imap_username=payload.imap_username,
                credential=payload.credential,
            )
        )
    except MailInboxValidationError as exc:
        raise DashboardBadRequestError(str(exc), code="invalid_mail_account") from exc
    return _account_response(account)


@router.patch("/accounts/{account_id}", response_model=MailAccountResponse)
async def update_mail_account(
    account_id: str,
    payload: MailAccountUpdateRequest = Body(...),
    context: MailInboxContext = Depends(get_mail_inbox_context),
) -> MailAccountResponse:
    try:
        account = await context.service.update_account(
            account_id,
            enabled=payload.enabled,
            display_name=payload.display_name,
        )
    except MailInboxNotFoundError as exc:
        raise DashboardNotFoundError(str(exc), code="mail_account_not_found") from exc
    return _account_response(account)


@router.post("/accounts/{account_id}/sync", response_model=MailAccountSyncResponse)
async def sync_mail_account(
    account_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    context: MailInboxContext = Depends(get_mail_inbox_context),
) -> MailAccountSyncResponse:
    try:
        account, imported_count = await context.service.sync_account(account_id, limit=limit)
    except MailInboxNotFoundError as exc:
        raise DashboardNotFoundError(str(exc), code="mail_account_not_found") from exc
    except MailInboxValidationError as exc:
        raise DashboardBadRequestError(str(exc), code="mail_sync_failed") from exc
    return MailAccountSyncResponse(account=_account_response(account), imported_count=imported_count)


@router.get("/messages", response_model=MailMessagesResponse)
async def list_mail_messages(
    account_id: str | None = Query(default=None, alias="accountId"),
    focused: bool | None = None,
    unread: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    context: MailInboxContext = Depends(get_mail_inbox_context),
) -> MailMessagesResponse:
    messages = await context.service.list_messages(account_id=account_id, focused=focused, unread=unread, limit=limit)
    return MailMessagesResponse(messages=[_message_response(message) for message in messages])


@router.post("/messages", response_model=MailMessageResponse)
async def ingest_mail_message(
    payload: MailMessageIngestRequest = Body(...),
    context: MailInboxContext = Depends(get_mail_inbox_context),
) -> MailMessageResponse:
    try:
        message = await context.service.ingest_message(
            MailMessageIngestData(
                account_id=payload.account_id,
                provider_message_id=payload.provider_message_id,
                thread_id=payload.thread_id,
                sender_email=payload.sender_email,
                sender_name=payload.sender_name,
                recipients=payload.recipients,
                subject=payload.subject,
                snippet=payload.snippet,
                received_at=payload.received_at,
                unread=payload.unread,
                starred=payload.starred,
                has_attachments=payload.has_attachments,
            )
        )
    except MailInboxNotFoundError as exc:
        raise DashboardNotFoundError(str(exc), code="mail_account_not_found") from exc
    except MailInboxValidationError as exc:
        raise DashboardBadRequestError(str(exc), code="invalid_mail_message") from exc
    return _message_response(message)


@router.get("/focus-rules", response_model=MailFocusRulesResponse)
async def list_mail_focus_rules(context: MailInboxContext = Depends(get_mail_inbox_context)) -> MailFocusRulesResponse:
    rules = await context.service.list_focus_rules()
    return MailFocusRulesResponse(rules=[_rule_response(rule) for rule in rules])


@router.post("/focus-rules", response_model=MailFocusRuleResponse)
async def create_mail_focus_rule(
    payload: MailFocusRuleCreateRequest = Body(...),
    context: MailInboxContext = Depends(get_mail_inbox_context),
) -> MailFocusRuleResponse:
    try:
        rule = await context.service.create_focus_rule(kind=payload.kind, value=payload.value, label=payload.label)
    except MailInboxValidationError as exc:
        raise DashboardBadRequestError(str(exc), code="invalid_focus_rule") from exc
    return _rule_response(rule)


@router.patch("/focus-rules/{rule_id}", response_model=MailFocusRuleResponse)
async def update_mail_focus_rule(
    rule_id: str,
    payload: MailFocusRuleUpdateRequest = Body(...),
    context: MailInboxContext = Depends(get_mail_inbox_context),
) -> MailFocusRuleResponse:
    try:
        rule = await context.service.update_focus_rule(rule_id, enabled=payload.enabled)
    except MailInboxNotFoundError as exc:
        raise DashboardNotFoundError(str(exc), code="focus_rule_not_found") from exc
    return _rule_response(rule)
