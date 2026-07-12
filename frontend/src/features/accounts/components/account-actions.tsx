import {
  Pause,
  Play,
  RefreshCw,
  RotateCcw,
  Sparkles,
  Trash2,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import type {
  AccountRateLimitResetCreditsResponse,
  AccountSummary,
} from "@/features/accounts/schemas";

export type AccountActionsProps = {
  account: AccountSummary;
  busy: boolean;
  onPause: (accountId: string) => void;
  onResume: (accountId: string) => void;
  onDelete: (accountId: string) => void;
  onReauth: (accountId: string) => void;
  resetCredits: AccountRateLimitResetCreditsResponse | null;
  resetCreditsLoading: boolean;
  resetCreditsError: boolean;
  resetBusy: boolean;
  onRefreshResetCredits: () => void;
  onUseRateLimitReset: (accountId: string) => void;
};

export function AccountActions({
  account,
  busy,
  onPause,
  onResume,
  onDelete,
  onReauth,
  resetCredits,
  resetCreditsLoading,
  resetCreditsError,
  resetBusy,
  onRefreshResetCredits,
  onUseRateLimitReset,
}: AccountActionsProps) {
  const resetCreditCount = resetCredits?.availableCount ?? 0;
  const resetSupported = account.providerKind !== "api_key";
  const reauthSupported = account.providerKind !== "api_key";
  const resetDisabled =
    busy || resetBusy || resetCreditsLoading || resetCreditCount <= 0;

  return (
    <div className="space-y-3 border-t pt-4">
      {resetSupported ? (
        <div className="flex flex-col gap-3 rounded-lg border border-emerald-500/20 bg-[linear-gradient(135deg,rgba(16,185,129,0.12),rgba(59,130,246,0.08))] px-3 py-2.5 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex min-w-0 items-center gap-2.5">
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-emerald-500/25 bg-background/70 text-emerald-500">
              <Sparkles className="h-4 w-4" />
            </span>
            <div className="min-w-0">
              <p className="text-xs font-semibold text-foreground">
                Rate-limit resets
              </p>
              <p className="text-xs text-muted-foreground">
                {resetCreditsLoading
                  ? "Checking available credits..."
                  : resetCreditsError
                    ? "Could not load available credits"
                    : `${resetCreditCount} available`}
              </p>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Button
              type="button"
              size="icon-sm"
              variant="outline"
              className="h-8 w-8 bg-background/70"
              onClick={onRefreshResetCredits}
              disabled={busy || resetCreditsLoading}
              title="Refresh reset credits"
              aria-label="Refresh reset credits"
            >
              <RefreshCw
                className={
                  resetCreditsLoading
                    ? "h-3.5 w-3.5 animate-spin"
                    : "h-3.5 w-3.5"
                }
              />
            </Button>
            <Button
              type="button"
              size="sm"
              className="h-8 gap-1.5 border border-emerald-400/30 bg-emerald-500 text-xs text-white shadow-sm hover:bg-emerald-500/90 dark:bg-emerald-500 dark:text-emerald-950 dark:hover:bg-emerald-400"
              onClick={() => onUseRateLimitReset(account.accountId)}
              disabled={resetDisabled}
            >
              <RotateCcw className="h-3.5 w-3.5" />
              Use reset
            </Button>
          </div>
        </div>
      ) : null}

      <div className="flex flex-wrap gap-2">
        {account.status === "paused" ? (
          <Button
            type="button"
            size="sm"
            className="h-8 gap-1.5 text-xs"
            onClick={() => onResume(account.accountId)}
            disabled={busy}
          >
            <Play className="h-3.5 w-3.5" />
            Resume
          </Button>
        ) : (
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="h-8 gap-1.5 text-xs"
            onClick={() => onPause(account.accountId)}
            disabled={busy}
          >
            <Pause className="h-3.5 w-3.5" />
            Pause
          </Button>
        )}

        {reauthSupported ? (
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="h-8 gap-1.5 text-xs"
            onClick={() => onReauth(account.accountId)}
            disabled={busy}
          >
            <RefreshCw className="h-3.5 w-3.5" />
            Re-authenticate
          </Button>
        ) : null}

        <Button
          type="button"
          size="sm"
          variant="destructive"
          className="h-8 gap-1.5 text-xs"
          onClick={() => onDelete(account.accountId)}
          disabled={busy}
        >
          <Trash2 className="h-3.5 w-3.5" />
          Delete
        </Button>
      </div>
    </div>
  );
}
