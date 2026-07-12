import { get, patch, post } from "@/lib/api-client";

import {
  MailAccountSchema,
  MailAccountSyncResponseSchema,
  MailAccountsResponseSchema,
  MailFocusRuleSchema,
  MailFocusRulesResponseSchema,
  MailMessagesResponseSchema,
  type MailAccountCreatePayload,
  type MailFocusRuleCreatePayload,
} from "@/features/mail/schemas";

const MAIL_PATH = "/api/mail";

export function getMailAccounts() {
  return get(`${MAIL_PATH}/accounts`, MailAccountsResponseSchema);
}

export function createMailAccount(payload: MailAccountCreatePayload) {
  return post(`${MAIL_PATH}/accounts`, MailAccountSchema, { body: payload });
}

export function syncMailAccount(accountId: string) {
  return post(`${MAIL_PATH}/accounts/${accountId}/sync`, MailAccountSyncResponseSchema);
}

export function getMailMessages(params?: {
  accountId?: string | null;
  focused?: boolean | null;
  unread?: boolean | null;
}) {
  const search = new URLSearchParams();
  if (params?.accountId) search.set("accountId", params.accountId);
  if (params?.focused !== undefined && params.focused !== null) search.set("focused", String(params.focused));
  if (params?.unread !== undefined && params.unread !== null) search.set("unread", String(params.unread));
  const suffix = search.toString() ? `?${search.toString()}` : "";
  return get(`${MAIL_PATH}/messages${suffix}`, MailMessagesResponseSchema);
}

export function getMailFocusRules() {
  return get(`${MAIL_PATH}/focus-rules`, MailFocusRulesResponseSchema);
}

export function createMailFocusRule(payload: MailFocusRuleCreatePayload) {
  return post(`${MAIL_PATH}/focus-rules`, MailFocusRuleSchema, { body: payload });
}

export function updateMailFocusRule(ruleId: string, payload: { enabled: boolean }) {
  return patch(`${MAIL_PATH}/focus-rules/${ruleId}`, MailFocusRuleSchema, { body: payload });
}
