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
import { Crown, KeyRound, ShieldCheck, Sparkles, type LucideIcon } from "lucide-react";

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

const ACCOUNT_PROVIDER_API_KEY = "api_key";

type AccountListVisual = {
  label: string;
  subtitle: string;
  icon: LucideIcon;
  rowClassName: string;
  selectedClassName: string;
  titleClassName?: string;
  iconClassName: string;
  badgeClassName: string;
  railClassName: string;
};

function accountListVisual(account: AccountSummary): AccountListVisual {
  const plan = account.planType.trim().toLowerCase();

  if (account.providerKind === ACCOUNT_PROVIDER_API_KEY) {
    return {
      label: "Provider",
      subtitle: "Dedicated API key lane",
      icon: KeyRound,
      rowClassName:
        "border-cyan-500/12 bg-[linear-gradient(135deg,rgba(6,182,212,0.08),rgba(20,184,166,0.04),transparent_70%)] hover:border-cyan-400/28 hover:bg-cyan-950/10",
      selectedClassName:
        "border-cyan-400/45 bg-[linear-gradient(135deg,rgba(6,182,212,0.20),rgba(20,184,166,0.10),rgba(15,23,42,0.14))] ring-1 ring-cyan-400/30 shadow-[0_18px_46px_rgba(6,182,212,0.16)]",
      titleClassName: "text-cyan-50",
      iconClassName:
        "border-cyan-300/60 bg-cyan-400/12 text-cyan-200 shadow-[0_0_22px_rgba(6,182,212,0.26)]",
      badgeClassName: "border-cyan-300/40 bg-cyan-400/10 text-cyan-100",
      railClassName: "from-cyan-400 via-teal-300 to-emerald-300",
    };
  }

  if (plan === "pro") {
    return {
      label: account.kycEnabled ? "Pro + KYC" : "Pro",
      subtitle: "Priority high-capacity lane",
      icon: Sparkles,
      rowClassName:
        "border-violet-500/14 bg-[radial-gradient(circle_at_10%_20%,rgba(139,92,246,0.18),transparent_34%),linear-gradient(135deg,rgba(124,58,237,0.12),rgba(168,85,247,0.05),transparent_72%)] hover:border-violet-300/36 hover:bg-violet-950/10",
      selectedClassName:
        "border-violet-300/48 bg-[radial-gradient(circle_at_10%_20%,rgba(167,139,250,0.25),transparent_36%),linear-gradient(135deg,rgba(124,58,237,0.22),rgba(168,85,247,0.10),rgba(15,23,42,0.14))] ring-1 ring-violet-300/35 shadow-[0_18px_50px_rgba(124,58,237,0.22)]",
      titleClassName:
        "bg-[linear-gradient(110deg,#ede9fe_0%,#c084fc_42%,#ffffff_55%,#8b5cf6_100%)] bg-clip-text font-semibold text-transparent drop-shadow-[0_0_10px_rgba(139,92,246,0.30)]",
      iconClassName:
        "border-violet-200/65 bg-violet-300/16 text-violet-100 shadow-[0_0_24px_rgba(139,92,246,0.36)]",
      badgeClassName: "border-violet-200/45 bg-violet-300/12 text-violet-50",
      railClassName: "from-violet-400 via-fuchsia-300 to-purple-300",
    };
  }

  if (account.kycEnabled) {
    return {
      label: `${formatSlug(plan)} + KYC`,
      subtitle: "Verified account lane",
      icon: ShieldCheck,
      rowClassName:
        "border-amber-500/12 bg-[linear-gradient(135deg,rgba(245,158,11,0.10),rgba(234,179,8,0.04),transparent_72%)] hover:border-amber-300/30 hover:bg-amber-950/10",
      selectedClassName:
        "border-amber-300/46 bg-[linear-gradient(135deg,rgba(245,158,11,0.20),rgba(234,179,8,0.10),rgba(15,23,42,0.14))] ring-1 ring-amber-300/32 shadow-[0_18px_48px_rgba(245,158,11,0.18)]",
      iconClassName:
        "border-amber-200/65 bg-amber-300/16 text-amber-100 shadow-[0_0_24px_rgba(245,158,11,0.34)]",
      badgeClassName: "border-amber-200/45 bg-amber-300/12 text-amber-50",
      railClassName: "from-amber-300 via-yellow-200 to-orange-400",
    };
  }

  if (plan === "plus") {
    return {
      label: "Plus",
      subtitle: "Standard paid lane",
      icon: Crown,
      rowClassName:
        "border-orange-500/10 bg-[linear-gradient(135deg,rgba(249,115,22,0.08),rgba(251,191,36,0.03),transparent_72%)] hover:border-orange-300/25 hover:bg-orange-950/10",
      selectedClassName:
        "border-orange-300/42 bg-[linear-gradient(135deg,rgba(249,115,22,0.16),rgba(251,191,36,0.08),rgba(15,23,42,0.12))] ring-1 ring-orange-300/26 shadow-[0_18px_44px_rgba(249,115,22,0.14)]",
      titleClassName: "text-orange-100",
      iconClassName:
        "border-orange-200/55 bg-orange-300/13 text-orange-100 shadow-[0_0_20px_rgba(249,115,22,0.25)]",
      badgeClassName: "border-orange-200/38 bg-orange-300/10 text-orange-50",
      railClassName: "from-orange-400 via-amber-300 to-yellow-200",
    };
  }

  return {
    label: "Free",
    subtitle: "General account lane",
    icon: ShieldCheck,
    rowClassName:
      "border-emerald-500/8 bg-[linear-gradient(135deg,rgba(16,185,129,0.06),rgba(15,23,42,0.02),transparent_74%)] hover:border-emerald-300/20 hover:bg-emerald-950/8",
    selectedClassName:
      "border-emerald-300/35 bg-[linear-gradient(135deg,rgba(16,185,129,0.14),rgba(15,23,42,0.12),transparent_76%)] ring-1 ring-emerald-300/24 shadow-[0_18px_40px_rgba(16,185,129,0.12)]",
    titleClassName: "text-foreground",
    iconClassName: "border-emerald-300/38 bg-emerald-400/10 text-emerald-100",
    badgeClassName: "border-emerald-300/30 bg-emerald-400/8 text-emerald-50",
    railClassName: "from-emerald-400 via-teal-300 to-cyan-300",
  };
}

export function AccountListItem({ account, selected, showAccountId = false, onSelect }: AccountListItemProps) {
  const blurred = usePrivacyStore((s) => s.blurred);
  const status = normalizeStatus(account.status);
  const title = account.displayName || account.email;
  const titleIsEmail = isEmailLabel(title, account.email);
  const emailSubtitle = account.displayName && account.displayName !== account.email
    ? account.email
    : null;
  const idSuffix = showAccountId ? ` | ID ${formatCompactAccountId(account.accountId)}` : "";
  const secondary = account.usage?.secondaryRemainingPercent ?? null;
  const visual = accountListVisual(account);
  const VisualIcon = visual.icon;
  const groupSummary = account.groups?.length ? `Groups ${account.groups.slice(0, 3).join(" / ")}` : null;
  const detailText = emailSubtitle
    ? `${emailSubtitle}${idSuffix}`
    : showAccountId
      ? `ID ${formatCompactAccountId(account.accountId)}`
      : groupSummary;

  return (
    <button
      type="button"
      onClick={() => onSelect(account.accountId)}
      className={cn(
        "group relative isolate w-full overflow-hidden rounded-xl border px-3 py-2.5 text-left transition-all duration-200",
        "before:absolute before:inset-y-2 before:left-0 before:w-1 before:rounded-r-full before:bg-gradient-to-b before:opacity-75 before:transition-opacity",
        visual.rowClassName,
        selected ? visual.selectedClassName : "shadow-none hover:translate-x-0.5",
        visual.railClassName,
      )}
    >
      <div
        aria-hidden="true"
        className={cn(
          "pointer-events-none absolute inset-y-0 right-0 w-1/2 opacity-0 blur-2xl transition-opacity duration-300 group-hover:opacity-70",
          selected ? "opacity-70" : undefined,
          `bg-gradient-to-l ${visual.railClassName} to-transparent`,
        )}
      />
      <div className="relative flex items-center gap-2.5">
        <span className={cn("inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full border", visual.iconClassName)}>
          <VisualIcon className="h-4 w-4" strokeWidth={2.25} />
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium">
            <KycAccountName kyc={account.kycEnabled} blurred={titleIsEmail && blurred} className={visual.titleClassName}>
              {title}
            </KycAccountName>
          </p>
          <div className="mt-0.5 flex min-w-0 items-center gap-1.5">
            <Badge variant="outline" className={cn("h-5 rounded-full px-2 text-[10px] font-semibold uppercase tracking-[0.12em]", visual.badgeClassName)}>
              {visual.label}
            </Badge>
            {detailText ? (
              <p className="min-w-0 truncate text-xs text-muted-foreground" title={showAccountId ? `Account ID ${account.accountId}` : undefined}>
                {emailSubtitle ? <><span className={blurred ? "privacy-blur" : undefined}>{emailSubtitle}</span>{idSuffix}</> : detailText}
              </p>
            ) : null}
          </div>
        </div>
        <StatusBadge status={status} />
      </div>
      <p className="relative mt-1.5 truncate pl-10 text-[10px] uppercase tracking-[0.16em] text-muted-foreground/60">
        {visual.subtitle}
      </p>
      <div className="relative mt-2">
        <MiniQuotaBar percent={secondary} />
      </div>
    </button>
  );
}
