import { z } from "zod";

export const MailProviderSchema = z.enum(["gmail", "outlook", "imap"]);
export const MailFocusRuleKindSchema = z.enum(["sender_email", "sender_domain", "keyword"]);

export const MailAccountSchema = z.object({
  id: z.string(),
  provider: MailProviderSchema,
  address: z.string(),
  displayName: z.string().nullable(),
  enabled: z.boolean(),
  syncStatus: z.enum(["never_synced", "synced", "error", "disabled"]),
  lastSyncAt: z.string().nullable(),
  lastSyncError: z.string().nullable(),
  imapHost: z.string().nullable(),
  imapPort: z.number().nullable(),
  imapUsername: z.string().nullable(),
  createdAt: z.string(),
  updatedAt: z.string(),
});

export const MailAccountsResponseSchema = z.object({
  accounts: z.array(MailAccountSchema),
});

export const MailAccountSyncResponseSchema = z.object({
  account: MailAccountSchema,
  importedCount: z.number(),
});

export const MailMessageSchema = z.object({
  id: z.string(),
  accountId: z.string(),
  accountAddress: z.string(),
  accountProvider: MailProviderSchema,
  providerMessageId: z.string(),
  threadId: z.string().nullable(),
  senderEmail: z.string(),
  senderName: z.string().nullable(),
  recipients: z.array(z.string()),
  subject: z.string(),
  snippet: z.string(),
  receivedAt: z.string(),
  unread: z.boolean(),
  starred: z.boolean(),
  hasAttachments: z.boolean(),
  focused: z.boolean(),
  focusLabel: z.string().nullable(),
});

export const MailMessagesResponseSchema = z.object({
  messages: z.array(MailMessageSchema),
});

export const MailFocusRuleSchema = z.object({
  id: z.string(),
  kind: MailFocusRuleKindSchema,
  value: z.string(),
  label: z.string().nullable(),
  enabled: z.boolean(),
  createdAt: z.string(),
  updatedAt: z.string(),
});

export const MailFocusRulesResponseSchema = z.object({
  rules: z.array(MailFocusRuleSchema),
});

export type MailProvider = z.infer<typeof MailProviderSchema>;
export type MailFocusRuleKind = z.infer<typeof MailFocusRuleKindSchema>;
export type MailAccount = z.infer<typeof MailAccountSchema>;
export type MailMessage = z.infer<typeof MailMessageSchema>;
export type MailFocusRule = z.infer<typeof MailFocusRuleSchema>;
export type MailAccountsResponse = z.infer<typeof MailAccountsResponseSchema>;
export type MailAccountSyncResponse = z.infer<typeof MailAccountSyncResponseSchema>;
export type MailMessagesResponse = z.infer<typeof MailMessagesResponseSchema>;
export type MailFocusRulesResponse = z.infer<typeof MailFocusRulesResponseSchema>;

export type MailAccountCreatePayload = {
  provider: MailProvider;
  address: string;
  displayName?: string | null;
  imapHost?: string | null;
  imapPort?: number | null;
  imapUsername?: string | null;
  credential?: string | null;
};

export type MailFocusRuleCreatePayload = {
  kind: MailFocusRuleKind;
  value: string;
  label?: string | null;
};
