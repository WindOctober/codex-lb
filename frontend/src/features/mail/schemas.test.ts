import { describe, expect, it } from "vitest";

import {
  MailAccountSyncResponseSchema,
  MailMessagesResponseSchema,
} from "@/features/mail/schemas";

describe("mail schemas", () => {
  it("parses focused mail messages", () => {
    const parsed = MailMessagesResponseSchema.parse({
      messages: [
        {
          id: "msg_1",
          accountId: "acct_1",
          accountAddress: "alerts@example.com",
          accountProvider: "gmail",
          providerMessageId: "provider_1",
          threadId: null,
          senderEmail: "support@openai.com",
          senderName: "OpenAI Support",
          recipients: ["alerts@example.com"],
          subject: "Case update",
          snippet: "Your case has an update",
          receivedAt: "2026-06-19T08:00:00Z",
          unread: true,
          starred: false,
          hasAttachments: false,
          focused: true,
          focusLabel: "OpenAI",
        },
      ],
    });

    expect(parsed.messages[0]?.focused).toBe(true);
    expect(parsed.messages[0]?.focusLabel).toBe("OpenAI");
  });

  it("parses mailbox sync responses", () => {
    const parsed = MailAccountSyncResponseSchema.parse({
      account: {
        id: "acct_1",
        provider: "gmail",
        address: "alerts@example.com",
        displayName: null,
        enabled: true,
        syncStatus: "synced",
        lastSyncAt: "2026-06-19T08:00:00Z",
        lastSyncError: null,
        imapHost: null,
        imapPort: null,
        imapUsername: null,
        createdAt: "2026-06-19T07:00:00Z",
        updatedAt: "2026-06-19T08:00:00Z",
      },
      importedCount: 1,
    });

    expect(parsed.account.syncStatus).toBe("synced");
    expect(parsed.importedCount).toBe(1);
  });
});
