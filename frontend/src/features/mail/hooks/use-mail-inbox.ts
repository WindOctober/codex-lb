import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  createMailAccount,
  createMailFocusRule,
  getMailAccounts,
  getMailFocusRules,
  getMailMessages,
  syncMailAccount,
  updateMailFocusRule,
} from "@/features/mail/api";
import type { MailAccountCreatePayload, MailFocusRuleCreatePayload } from "@/features/mail/schemas";
import { getErrorMessageOrNull } from "@/utils/errors";

export const MAIL_ACCOUNTS_QUERY_KEY = ["mail", "accounts"] as const;
export const MAIL_MESSAGES_QUERY_KEY = ["mail", "messages"] as const;
export const MAIL_FOCUS_RULES_QUERY_KEY = ["mail", "focus-rules"] as const;

export type MailMessageFilter = "focused" | "all" | "unread";

function messageParams(filter: MailMessageFilter, accountId: string | null) {
  return {
    accountId,
    focused: filter === "focused" ? true : null,
    unread: filter === "unread" ? true : null,
  };
}

export function useMailAccounts() {
  return useQuery({
    queryKey: MAIL_ACCOUNTS_QUERY_KEY,
    queryFn: getMailAccounts,
  });
}

export function useMailMessages(filter: MailMessageFilter, accountId: string | null) {
  return useQuery({
    queryKey: [...MAIL_MESSAGES_QUERY_KEY, filter, accountId] as const,
    queryFn: () => getMailMessages(messageParams(filter, accountId)),
  });
}

export function useMailFocusRules() {
  return useQuery({
    queryKey: MAIL_FOCUS_RULES_QUERY_KEY,
    queryFn: getMailFocusRules,
  });
}

export function useCreateMailAccount() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: MailAccountCreatePayload) => createMailAccount(payload),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: MAIL_ACCOUNTS_QUERY_KEY });
      toast.success("Mail account added");
    },
    onError: (error) => {
      toast.error(getErrorMessageOrNull(error) ?? "Failed to add mail account");
    },
  });
}

export function useSyncMailAccount() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (accountId: string) => syncMailAccount(accountId),
    onSuccess: async (result) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: MAIL_ACCOUNTS_QUERY_KEY }),
        queryClient.invalidateQueries({ queryKey: MAIL_MESSAGES_QUERY_KEY }),
      ]);
      toast.success(`Imported ${result.importedCount} mail messages`);
    },
    onError: (error) => {
      toast.error(getErrorMessageOrNull(error) ?? "Failed to sync mailbox");
    },
  });
}

export function useCreateMailFocusRule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: MailFocusRuleCreatePayload) => createMailFocusRule(payload),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: MAIL_FOCUS_RULES_QUERY_KEY }),
        queryClient.invalidateQueries({ queryKey: MAIL_MESSAGES_QUERY_KEY }),
      ]);
      toast.success("Focus rule added");
    },
    onError: (error) => {
      toast.error(getErrorMessageOrNull(error) ?? "Failed to add focus rule");
    },
  });
}

export function useToggleMailFocusRule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ ruleId, enabled }: { ruleId: string; enabled: boolean }) =>
      updateMailFocusRule(ruleId, { enabled }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: MAIL_FOCUS_RULES_QUERY_KEY }),
        queryClient.invalidateQueries({ queryKey: MAIL_MESSAGES_QUERY_KEY }),
      ]);
    },
    onError: (error) => {
      toast.error(getErrorMessageOrNull(error) ?? "Failed to update focus rule");
    },
  });
}
