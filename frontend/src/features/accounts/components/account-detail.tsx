import { CalendarClock, Save, Star, User, X } from "lucide-react";
import { useMemo, useState } from "react";

import { isEmailLabel } from "@/components/blur-email";
import { KycAccountName } from "@/components/kyc-account-name";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { usePrivacyStore } from "@/hooks/use-privacy";
import { AccountActions } from "@/features/accounts/components/account-actions";
import { AccountRoutingPanel } from "@/features/accounts/components/account-routing-panel";
import { AccountTokenInfo } from "@/features/accounts/components/account-token-info";
import { AccountUsagePanel } from "@/features/accounts/components/account-usage-panel";
import type { AccountSummary } from "@/features/accounts/schemas";
import {
  useAccountResetCredits,
  useAccountTrends,
} from "@/features/accounts/hooks/use-accounts";
import { formatCompactAccountId } from "@/utils/account-identifiers";

export type AccountDetailProps = {
  account: AccountSummary | null;
  showAccountId?: boolean;
  busy: boolean;
  onPause: (accountId: string) => void;
  onResume: (accountId: string) => void;
  onDelete: (accountId: string) => void;
  onReauth: (accountId: string) => void;
  onUseRateLimitReset: (accountId: string) => void;
  resetBusy?: boolean;
  onUpdateRouting: (
    accountId: string,
    configuredPriority: number,
    kycEnabled?: boolean,
    fastServiceTierEnabled?: boolean,
    groups?: string[],
    primaryDrainPriorityEnabled?: boolean,
    subscriptionRenewsAt?: string | null,
  ) => Promise<void>;
  onTestAvailability: (accountId: string) => Promise<void>;
};

export function AccountDetail({
  account,
  showAccountId = false,
  busy,
  onPause,
  onResume,
  onDelete,
  onReauth,
  onUseRateLimitReset,
  resetBusy = false,
  onUpdateRouting,
  onTestAvailability,
}: AccountDetailProps) {
  const { data: trends } = useAccountTrends(account?.accountId ?? null);
  const resetCreditsQuery = useAccountResetCredits(
    account?.accountId ?? null,
    account?.providerKind !== "api_key",
  );
  const blurred = usePrivacyStore((s) => s.blurred);
  const savedRenewalInput = useMemo(
    () => toDateInputValue(account?.subscriptionRenewsAt),
    [account?.subscriptionRenewsAt],
  );

  if (!account) {
    return (
      <div className="flex flex-col items-center justify-center rounded-xl border border-dashed p-12">
        <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-muted">
          <User className="h-5 w-5 text-muted-foreground" />
        </div>
        <p className="mt-3 text-sm font-medium text-muted-foreground">
          Select an account
        </p>
        <p className="mt-1 text-xs text-muted-foreground/70">
          Choose an account from the list to view details.
        </p>
      </div>
    );
  }

  const title = account.displayName || account.email;
  const titleIsEmail = isEmailLabel(title, account.email);
  const compactId = formatCompactAccountId(account.accountId);
  const emailSubtitle =
    account.displayName && account.displayName !== account.email
      ? account.email
      : null;
  const idSuffix = showAccountId ? ` (${compactId})` : "";
  const drainPriorityEnabled =
    account.primaryDrainPriorityEnabled ?? false;

  return (
    <div
      key={account.accountId}
      className="animate-fade-in-up space-y-4 rounded-xl border bg-card p-5"
    >
      {/* Account header */}
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-base font-semibold">
            {titleIsEmail ? (
              <>
                <KycAccountName kyc={account.kycEnabled} blurred={blurred}>
                  {title}
                </KycAccountName>
                {idSuffix}
              </>
            ) : (
              <>
                <KycAccountName kyc={account.kycEnabled}>
                  {title}
                </KycAccountName>
                {!emailSubtitle ? idSuffix : ""}
              </>
            )}
          </h2>
          {emailSubtitle ? (
            <p
              className="mt-0.5 text-xs text-muted-foreground"
              title={
                showAccountId ? `Account ID ${account.accountId}` : undefined
              }
            >
              <span className={blurred ? "privacy-blur" : ""}>
                {emailSubtitle}
              </span>
              {showAccountId ? ` | ID ${compactId}` : ""}
            </p>
          ) : null}
        </div>
        <Button
          type="button"
          size="icon"
          variant={drainPriorityEnabled ? "default" : "outline"}
          className="h-8 w-8 shrink-0"
          disabled={busy}
          title="Toggle starred routing priority"
          aria-label={
            drainPriorityEnabled
              ? "Disable starred routing priority"
              : "Enable starred routing priority"
          }
          aria-pressed={drainPriorityEnabled}
          onClick={() =>
            void onUpdateRouting(
              account.accountId,
              account.configuredPriority ?? 0,
              account.kycEnabled ?? false,
              account.fastServiceTierEnabled ?? false,
              undefined,
              !drainPriorityEnabled,
            )
          }
        >
          <Star
            className={
              drainPriorityEnabled ? "h-4 w-4 fill-current" : "h-4 w-4"
            }
          />
        </Button>
      </div>

      <AccountUsagePanel account={account} trends={trends} />
      <AccountRoutingPanel
        key={`${account.accountId}:${account.configuredPriority ?? 0}:${(account.groups ?? []).join(",")}`}
        account={account}
        busy={busy}
        onUpdateRouting={onUpdateRouting}
        onTestAvailability={onTestAvailability}
      />
      <AccountSubscriptionPanel
        key={`${account.accountId}:${savedRenewalInput}`}
        account={account}
        busy={busy}
        savedRenewalInput={savedRenewalInput}
        onUpdateRouting={onUpdateRouting}
      />
      <AccountTokenInfo account={account} />
      <AccountActions
        account={account}
        busy={busy}
        onPause={onPause}
        onResume={onResume}
        onDelete={onDelete}
        onReauth={onReauth}
        resetCredits={resetCreditsQuery.data ?? null}
        resetCreditsLoading={
          resetCreditsQuery.isPending || resetCreditsQuery.isFetching
        }
        resetCreditsError={Boolean(resetCreditsQuery.error)}
        resetBusy={resetBusy}
        onRefreshResetCredits={() => void resetCreditsQuery.refetch()}
        onUseRateLimitReset={onUseRateLimitReset}
      />
    </div>
  );
}

type AccountSubscriptionPanelProps = {
  account: AccountSummary;
  busy: boolean;
  savedRenewalInput: string;
  onUpdateRouting: AccountDetailProps["onUpdateRouting"];
};

function AccountSubscriptionPanel({
  account,
  busy,
  savedRenewalInput,
  onUpdateRouting,
}: AccountSubscriptionPanelProps) {
  const [renewalInput, setRenewalInput] = useState(savedRenewalInput);
  const renewalIso = fromDateInputValue(renewalInput);
  const changed = renewalInput !== savedRenewalInput;
  const canSave = changed && renewalIso !== null;
  const canClear = Boolean(account.subscriptionRenewsAt || renewalInput);

  return (
    <section className="rounded-lg border bg-muted/20 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <CalendarClock className="h-4 w-4 text-muted-foreground" />
          <h3 className="text-sm font-semibold">Subscription renewal</h3>
        </div>
        <span className="text-xs text-muted-foreground">
          {formatSubscriptionRenewal(account.subscriptionRenewsAt)}
        </span>
      </div>

      <div className="mt-3 grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto_auto] sm:items-end">
        <div className="space-y-1.5">
          <Label htmlFor={`subscription-renews-at-${account.accountId}`}>
            Renews on
          </Label>
          <Input
            id={`subscription-renews-at-${account.accountId}`}
            type="date"
            value={renewalInput}
            onChange={(event) => setRenewalInput(event.target.value)}
          />
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-9 gap-1.5"
          disabled={busy || !canSave}
          onClick={() =>
            void onUpdateRouting(
              account.accountId,
              account.configuredPriority ?? 0,
              account.kycEnabled ?? false,
              account.fastServiceTierEnabled ?? false,
              undefined,
              account.primaryDrainPriorityEnabled ?? false,
              renewalIso,
            )
          }
        >
          <Save className="h-3.5 w-3.5" />
          Save
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-9 gap-1.5"
          disabled={busy || !canClear}
          onClick={() =>
            void onUpdateRouting(
              account.accountId,
              account.configuredPriority ?? 0,
              account.kycEnabled ?? false,
              account.fastServiceTierEnabled ?? false,
              undefined,
              account.primaryDrainPriorityEnabled ?? false,
              null,
            )
          }
        >
          <X className="h-3.5 w-3.5" />
          Clear
        </Button>
      </div>
    </section>
  );
}

function toDateInputValue(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toISOString().slice(0, 10);
}

function fromDateInputValue(value: string): string | null {
  if (!value) return null;
  const date = new Date(`${value}T00:00:00.000Z`);
  if (Number.isNaN(date.getTime())) return null;
  return date.toISOString();
}

function formatSubscriptionRenewal(value: string | null | undefined): string {
  if (!value) return "Not set";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Not set";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeZone: "UTC",
  }).format(date);
}
