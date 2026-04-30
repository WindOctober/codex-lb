import { cn } from "@/lib/utils";
import { isEmailLabel } from "@/components/blur-email";
import { KycAccountName } from "@/components/kyc-account-name";
import { usePrivacyStore } from "@/hooks/use-privacy";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import type { AccountSummary } from "@/features/accounts/schemas";
import { normalizeStatus, quotaBarColor, quotaBarTrack } from "@/utils/account-status";
import { formatCompactAccountId } from "@/utils/account-identifiers";
import { formatSlug } from "@/utils/formatters";

export type AccountListItemProps = {
  account: AccountSummary;
  selected: boolean;
  showAccountId?: boolean;
  onSelect: (accountId: string) => void;
};

function MiniQuotaBar({ percent }: { percent: number | null }) {
  if (percent === null) {
    return <div data-testid="mini-quota-track" className="h-1 flex-1 overflow-hidden rounded-full bg-muted" />;
  }
  const clamped = Math.max(0, Math.min(100, percent));
  return (
    <div data-testid="mini-quota-track" className={cn("h-1 flex-1 overflow-hidden rounded-full", quotaBarTrack(clamped))}>
      <div
        data-testid="mini-quota-fill"
        className={cn("h-full rounded-full", quotaBarColor(clamped))}
        style={{ width: `${clamped}%` }}
      />
    </div>
  );
}

export function AccountListItem({ account, selected, showAccountId = false, onSelect }: AccountListItemProps) {
  const blurred = usePrivacyStore((s) => s.blurred);
  const status = normalizeStatus(account.status);
  const title = account.displayName || account.email;
  const titleIsEmail = isEmailLabel(title, account.email);
  const emailSubtitle = account.displayName && account.displayName !== account.email
    ? account.email
    : null;
  const baseSubtitle = emailSubtitle ?? formatSlug(account.planType);
  const idSuffix = showAccountId ? ` | ID ${formatCompactAccountId(account.accountId)}` : "";
  const secondary = account.usage?.secondaryRemainingPercent ?? null;

  return (
    <button
      type="button"
      onClick={() => onSelect(account.accountId)}
      className={cn(
        "w-full rounded-lg px-3 py-2.5 text-left transition-colors",
        selected
          ? "bg-primary/8 ring-1 ring-primary/25"
          : "hover:bg-muted/50",
      )}
    >
      <div className="flex items-center gap-2.5">
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium">
            <KycAccountName kyc={account.kycEnabled} blurred={titleIsEmail && blurred}>
              {title}
            </KycAccountName>
          </p>
          <p className="truncate text-xs text-muted-foreground" title={showAccountId ? `Account ID ${account.accountId}` : undefined}>
            {emailSubtitle ? <><span className={blurred ? "privacy-blur" : undefined}>{emailSubtitle}</span>{idSuffix}</> : <>{baseSubtitle}{idSuffix}</>}
          </p>
        </div>
        {account.kycEnabled ? (
          <Badge
            variant="outline"
            className="border-amber-300/80 bg-[linear-gradient(135deg,rgba(251,191,36,0.28),rgba(255,247,173,0.18),rgba(217,119,6,0.24))] text-amber-800 shadow-[0_0_18px_rgba(245,158,11,0.30),inset_0_1px_0_rgba(255,255,255,0.28)] dark:border-amber-200/55 dark:bg-[linear-gradient(135deg,rgba(251,191,36,0.24),rgba(255,247,173,0.12),rgba(217,119,6,0.20))] dark:text-amber-100 dark:shadow-[0_0_18px_rgba(251,191,36,0.26),inset_0_1px_0_rgba(255,255,255,0.12)]"
          >
            KYC
          </Badge>
        ) : null}
        <StatusBadge status={status} />
      </div>
      <div className="mt-1.5">
        <MiniQuotaBar percent={secondary} />
      </div>
    </button>
  );
}
