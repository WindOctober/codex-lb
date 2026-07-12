import { Bell, Inbox, Mail, Plus, RefreshCw, ShieldCheck, Star, Tag } from "lucide-react";
import { useMemo, useState, type FormEvent } from "react";

import { AlertMessage } from "@/components/alert-message";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  type MailMessageFilter,
  useCreateMailAccount,
  useCreateMailFocusRule,
  useMailAccounts,
  useMailFocusRules,
  useMailMessages,
  useSyncMailAccount,
  useToggleMailFocusRule,
} from "@/features/mail/hooks/use-mail-inbox";
import type { MailAccount, MailFocusRuleKind, MailMessage, MailProvider } from "@/features/mail/schemas";
import { cn } from "@/lib/utils";
import { getErrorMessageOrNull } from "@/utils/errors";

const FILTERS: Array<{ key: MailMessageFilter; label: string; icon: typeof Inbox }> = [
  { key: "focused", label: "Focused", icon: Star },
  { key: "all", label: "All", icon: Inbox },
  { key: "unread", label: "Unread", icon: Bell },
];

function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function providerLabel(provider: MailProvider): string {
  if (provider === "gmail") return "Gmail";
  if (provider === "outlook") return "Outlook";
  return "IMAP";
}

function accountSyncLabel(account: MailAccount): string {
  if (account.syncStatus === "synced" && account.lastSyncAt) {
    return `last synced ${formatDateTime(account.lastSyncAt)}`;
  }
  return account.syncStatus.replace("_", " ");
}

function ruleKindLabel(kind: MailFocusRuleKind): string {
  if (kind === "sender_email") return "Sender";
  if (kind === "sender_domain") return "Domain";
  return "Keyword";
}

function messageTitle(message: MailMessage): string {
  return message.senderName ? `${message.senderName} <${message.senderEmail}>` : message.senderEmail;
}

export function MailPage() {
  const [filter, setFilter] = useState<MailMessageFilter>("focused");
  const [accountId, setAccountId] = useState<string | null>(null);
  const accountsQuery = useMailAccounts();
  const messagesQuery = useMailMessages(filter, accountId);
  const rulesQuery = useMailFocusRules();
  const createAccount = useCreateMailAccount();
  const createRule = useCreateMailFocusRule();
  const syncAccount = useSyncMailAccount();
  const toggleRule = useToggleMailFocusRule();
  const [selectedMessageId, setSelectedMessageId] = useState<string | null>(null);
  const [accountProvider, setAccountProvider] = useState<MailProvider>("imap");
  const [accountAddress, setAccountAddress] = useState("");
  const [imapHost, setImapHost] = useState("imap.163.com");
  const [imapPort, setImapPort] = useState("993");
  const [imapUsername, setImapUsername] = useState("");
  const [credential, setCredential] = useState("");
  const [ruleKind, setRuleKind] = useState<MailFocusRuleKind>("sender_domain");
  const [ruleValue, setRuleValue] = useState("");
  const [ruleLabel, setRuleLabel] = useState("");

  const accounts = useMemo(
    () => accountsQuery.data?.accounts ?? [],
    [accountsQuery.data],
  );
  const messages = useMemo(
    () => messagesQuery.data?.messages ?? [],
    [messagesQuery.data],
  );
  const rules = useMemo(
    () => rulesQuery.data?.rules ?? [],
    [rulesQuery.data],
  );
  const selectedMessage = useMemo(
    () => messages.find((message) => message.id === selectedMessageId) ?? messages[0] ?? null,
    [messages, selectedMessageId],
  );

  function submitAccount(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    createAccount.mutate({
      provider: accountProvider,
      address: accountAddress,
      imapHost: accountProvider === "imap" ? imapHost : null,
      imapPort: accountProvider === "imap" ? Number(imapPort) : null,
      imapUsername: accountProvider === "imap" ? imapUsername || accountAddress : null,
      credential: credential || null,
    }, {
      onSuccess: () => {
        setAccountAddress("");
        setCredential("");
      },
    });
  }

  function submitRule(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    createRule.mutate({
      kind: ruleKind,
      value: ruleValue,
      label: ruleLabel || null,
    }, {
      onSuccess: () => {
        setRuleValue("");
        setRuleLabel("");
      },
    });
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Mail</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Unified read-only inbox for support and subscription mail.
          </p>
        </div>
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <ShieldCheck className="h-4 w-4 text-emerald-500" />
          Read-only
        </div>
      </div>

      {(accountsQuery.error || messagesQuery.error || rulesQuery.error) ? (
        <AlertMessage variant="error">
          {
            getErrorMessageOrNull(accountsQuery.error ?? messagesQuery.error ?? rulesQuery.error) ??
            "Failed to load mail inbox data"
          }
        </AlertMessage>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-[280px_minmax(0,1fr)_minmax(320px,0.85fr)]">
        <aside className="space-y-4">
          <section className="rounded-lg border bg-card p-3">
            <h2 className="text-sm font-semibold">Views</h2>
            <div className="mt-3 grid gap-1">
              {FILTERS.map((item) => {
                const Icon = item.icon;
                return (
                  <button
                    key={item.key}
                    type="button"
                    className={cn(
                      "flex h-9 items-center gap-2 rounded-md px-2 text-left text-sm transition-colors",
                      filter === item.key
                        ? "bg-primary/10 text-primary"
                        : "text-muted-foreground hover:bg-muted hover:text-foreground",
                    )}
                    onClick={() => setFilter(item.key)}
                  >
                    <Icon className="h-4 w-4" />
                    {item.label}
                  </button>
                );
              })}
            </div>

            <Label className="mt-4 block text-xs" htmlFor="mail-account-filter">
              Account
            </Label>
            <Select value={accountId ?? "all"} onValueChange={(value) => setAccountId(value === "all" ? null : value)}>
              <SelectTrigger id="mail-account-filter" className="mt-1 h-9">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All accounts</SelectItem>
                {accounts.map((account) => (
                  <SelectItem key={account.id} value={account.id}>
                    {account.address}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            {accounts.length > 0 ? (
              <div className="mt-3 space-y-2">
                {accounts.map((account) => (
                  <div key={account.id} className="rounded-md border px-2 py-1.5">
                    <div className="flex items-center justify-between gap-2">
                      <div className="min-w-0">
                        <p className="truncate text-xs font-medium">{account.address}</p>
                        <p className="text-[11px] text-muted-foreground">
                          {providerLabel(account.provider)} - {accountSyncLabel(account)}
                        </p>
                      </div>
                      <Button
                        type="button"
                        aria-label={`Sync ${account.address}`}
                        size="sm"
                        variant="ghost"
                        className="h-7 px-2"
                        disabled={!account.enabled || (syncAccount.isPending && syncAccount.variables === account.id)}
                        onClick={() => syncAccount.mutate(account.id)}
                      >
                        <RefreshCw className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                    {account.lastSyncError ? (
                      <p className="mt-1 truncate text-[11px] text-destructive">{account.lastSyncError}</p>
                    ) : null}
                  </div>
                ))}
              </div>
            ) : null}
          </section>

          <section className="rounded-lg border bg-card p-3">
            <h2 className="text-sm font-semibold">Add Mailbox</h2>
            <form className="mt-3 space-y-2" onSubmit={submitAccount}>
              <Select value={accountProvider} onValueChange={(value) => setAccountProvider(value as MailProvider)}>
                <SelectTrigger className="h-9">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="imap">IMAP / 163</SelectItem>
                  <SelectItem value="gmail">Gmail</SelectItem>
                  <SelectItem value="outlook">Outlook</SelectItem>
                </SelectContent>
              </Select>
              <Input
                value={accountAddress}
                onChange={(event) => setAccountAddress(event.target.value)}
                placeholder="mail@example.com"
                type="email"
                required
              />
              {accountProvider === "imap" ? (
                <div className="grid grid-cols-[minmax(0,1fr)_72px] gap-2">
                  <Input value={imapHost} onChange={(event) => setImapHost(event.target.value)} placeholder="IMAP host" />
                  <Input value={imapPort} onChange={(event) => setImapPort(event.target.value)} placeholder="993" />
                  <Input
                    className="col-span-2"
                    value={imapUsername}
                    onChange={(event) => setImapUsername(event.target.value)}
                    placeholder="Username"
                  />
                  <Input
                    className="col-span-2"
                    value={credential}
                    onChange={(event) => setCredential(event.target.value)}
                    placeholder="App password"
                    type="password"
                  />
                </div>
              ) : null}
              <Button type="submit" size="sm" className="h-8 w-full gap-1.5" disabled={createAccount.isPending}>
                <Plus className="h-3.5 w-3.5" />
                Add
              </Button>
            </form>
          </section>

          <section className="rounded-lg border bg-card p-3">
            <h2 className="text-sm font-semibold">Focus Rules</h2>
            <form className="mt-3 space-y-2" onSubmit={submitRule}>
              <Select value={ruleKind} onValueChange={(value) => setRuleKind(value as MailFocusRuleKind)}>
                <SelectTrigger className="h-9">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="sender_email">Sender email</SelectItem>
                  <SelectItem value="sender_domain">Sender domain</SelectItem>
                  <SelectItem value="keyword">Keyword</SelectItem>
                </SelectContent>
              </Select>
              <Input value={ruleValue} onChange={(event) => setRuleValue(event.target.value)} placeholder="openai.com" required />
              <Input value={ruleLabel} onChange={(event) => setRuleLabel(event.target.value)} placeholder="Label" />
              <Button type="submit" size="sm" variant="outline" className="h-8 w-full gap-1.5" disabled={createRule.isPending}>
                <Tag className="h-3.5 w-3.5" />
                Track
              </Button>
            </form>
            <div className="mt-3 space-y-2">
              {rules.map((rule) => (
                <div key={rule.id} className="flex items-center justify-between gap-2 rounded-md border px-2 py-1.5">
                  <div className="min-w-0">
                    <p className="truncate text-xs font-medium">{rule.label || rule.value}</p>
                    <p className="text-[11px] text-muted-foreground">{ruleKindLabel(rule.kind)}</p>
                  </div>
                  <Button
                    type="button"
                    size="sm"
                    variant={rule.enabled ? "outline" : "ghost"}
                    className="h-7 text-xs"
                    onClick={() => toggleRule.mutate({ ruleId: rule.id, enabled: !rule.enabled })}
                  >
                    {rule.enabled ? "On" : "Off"}
                  </Button>
                </div>
              ))}
            </div>
          </section>
        </aside>

        <section className="min-h-[560px] rounded-lg border bg-card">
          <div className="flex h-12 items-center justify-between border-b px-4">
            <h2 className="text-sm font-semibold">{FILTERS.find((item) => item.key === filter)?.label} Mail</h2>
            <Badge variant="secondary">{messages.length}</Badge>
          </div>
          {messagesQuery.isLoading ? (
            <div className="space-y-3 p-4">
              <Skeleton className="h-16 w-full" />
              <Skeleton className="h-16 w-full" />
              <Skeleton className="h-16 w-full" />
            </div>
          ) : messages.length === 0 ? (
            <div className="flex min-h-[320px] flex-col items-center justify-center px-6 text-center">
              <Mail className="h-8 w-8 text-muted-foreground" />
              <p className="mt-3 text-sm font-medium">No messages</p>
              <p className="mt-1 text-xs text-muted-foreground">Messages will appear after mailbox sync or import.</p>
            </div>
          ) : (
            <div className="divide-y">
              {messages.map((message) => (
                <button
                  key={message.id}
                  type="button"
                  className={cn(
                    "grid w-full grid-cols-[4px_minmax(0,1fr)] text-left transition-colors hover:bg-muted/50",
                    selectedMessage?.id === message.id && "bg-muted/60",
                  )}
                  onClick={() => setSelectedMessageId(message.id)}
                >
                  <span className={message.focused ? "bg-amber-400" : "bg-transparent"} />
                  <span className="min-w-0 px-3 py-3">
                    <span className="flex items-center justify-between gap-3">
                      <span className="min-w-0 truncate text-sm font-medium">{messageTitle(message)}</span>
                      <span className="shrink-0 text-[11px] text-muted-foreground">{formatDateTime(message.receivedAt)}</span>
                    </span>
                    <span className="mt-1 flex items-center gap-2">
                      {message.focused ? (
                        <Badge className="h-5 bg-amber-500 text-[11px] text-white hover:bg-amber-500">
                          {message.focusLabel || "Focused"}
                        </Badge>
                      ) : null}
                      {message.unread ? <Badge variant="secondary" className="h-5 text-[11px]">Unread</Badge> : null}
                      <span className="min-w-0 truncate text-xs text-muted-foreground">{message.subject}</span>
                    </span>
                    <span className="mt-1 block truncate text-xs text-muted-foreground">{message.snippet}</span>
                  </span>
                </button>
              ))}
            </div>
          )}
        </section>

        <section className="rounded-lg border bg-card">
          <div className="flex h-12 items-center border-b px-4">
            <h2 className="text-sm font-semibold">Message</h2>
          </div>
          {selectedMessage ? (
            <div className="space-y-4 p-4">
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  {selectedMessage.focused ? (
                    <Badge className="bg-amber-500 text-white hover:bg-amber-500">
                      {selectedMessage.focusLabel || "Focused"}
                    </Badge>
                  ) : null}
                  <Badge variant="outline">{providerLabel(selectedMessage.accountProvider)}</Badge>
                  <Badge variant="outline">{selectedMessage.accountAddress}</Badge>
                </div>
                <h3 className="mt-3 text-base font-semibold leading-snug">{selectedMessage.subject}</h3>
                <p className="mt-2 text-sm text-muted-foreground">{messageTitle(selectedMessage)}</p>
                <p className="mt-1 text-xs text-muted-foreground">{formatDateTime(selectedMessage.receivedAt)}</p>
              </div>
              <div className="rounded-lg border bg-muted/20 p-3 text-sm leading-6">
                {selectedMessage.snippet || "No preview available."}
              </div>
              {selectedMessage.recipients.length > 0 ? (
                <div>
                  <p className="text-xs font-medium text-muted-foreground">Recipients</p>
                  <p className="mt-1 text-sm">{selectedMessage.recipients.join(", ")}</p>
                </div>
              ) : null}
            </div>
          ) : (
            <div className="flex min-h-[320px] items-center justify-center px-6 text-center text-sm text-muted-foreground">
              Select a message
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
