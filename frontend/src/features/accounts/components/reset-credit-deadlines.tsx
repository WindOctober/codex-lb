import { CalendarClock } from "lucide-react";

import { cn } from "@/lib/utils";
import type { AccountRateLimitResetCredit } from "@/features/accounts/schemas";

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

const deadlineFormatter = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  timeZoneName: "short",
});

type DeadlineState = {
  label: string;
  tone: string;
};

function formatDeadline(iso: string): string | null {
  const deadline = new Date(iso);
  return Number.isNaN(deadline.getTime())
    ? null
    : deadlineFormatter.format(deadline);
}

function getDeadlineState(
  expiresAt: string | null,
  now: number = Date.now(),
): DeadlineState {
  if (expiresAt === null) {
    return {
      label: "Deadline unavailable",
      tone: "border-border bg-muted text-muted-foreground",
    };
  }

  const deadline = new Date(expiresAt).getTime();
  if (Number.isNaN(deadline)) {
    return {
      label: "Deadline unavailable",
      tone: "border-border bg-muted text-muted-foreground",
    };
  }

  const remaining = deadline - now;
  if (remaining <= 0) {
    return {
      label: "Expired",
      tone:
        "border-red-500/25 bg-red-500/10 text-red-700 dark:text-red-300",
    };
  }

  let label: string;
  if (remaining < HOUR_MS) {
    label = `${Math.max(1, Math.ceil(remaining / MINUTE_MS))}m left`;
  } else if (remaining < DAY_MS) {
    label = `${Math.ceil(remaining / HOUR_MS)}h left`;
  } else {
    const days = Math.floor(remaining / DAY_MS);
    const hours = Math.floor((remaining % DAY_MS) / HOUR_MS);
    label = hours > 0 ? `${days}d ${hours}h left` : `${days}d left`;
  }

  if (remaining <= DAY_MS) {
    return {
      label,
      tone:
        "border-red-500/25 bg-red-500/10 text-red-700 dark:text-red-300",
    };
  }
  if (remaining <= 3 * DAY_MS) {
    return {
      label,
      tone:
        "border-amber-500/25 bg-amber-500/10 text-amber-700 dark:text-amber-300",
    };
  }
  return {
    label,
    tone:
      "border-emerald-500/25 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
  };
}

function getCreditTitle(credit: AccountRateLimitResetCredit): string {
  if (credit.title) return credit.title;
  if (credit.resetType) {
    return credit.resetType
      .split("_")
      .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
      .join(" ");
  }
  return "Rate-limit reset";
}

export type ResetCreditDeadlinesProps = {
  credits: AccountRateLimitResetCredit[];
};

export function ResetCreditDeadlines({ credits }: ResetCreditDeadlinesProps) {
  if (credits.length === 0) return null;

  return (
    <ul className="divide-y divide-border/70 border-t border-border/70">
      {credits.map((credit, index) => {
        const formattedDeadline = credit.expiresAt
          ? formatDeadline(credit.expiresAt)
          : null;
        const deadlineState = getDeadlineState(credit.expiresAt);

        return (
          <li
            key={`${credit.expiresAt ?? "unknown"}:${credit.grantedAt ?? "unknown"}:${index}`}
            className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] items-center gap-x-2.5 gap-y-1 px-3 py-2.5 sm:grid-cols-[auto_minmax(0,1fr)_auto]"
          >
            <CalendarClock
              className="h-4 w-4 text-muted-foreground"
              aria-hidden="true"
            />
            <div className="min-w-0">
              <p className="truncate text-xs font-medium text-foreground">
                {getCreditTitle(credit)}
              </p>
              {credit.expiresAt && formattedDeadline ? (
                <time
                  dateTime={credit.expiresAt}
                  className="block text-xs tabular-nums text-muted-foreground"
                >
                  Expires {formattedDeadline}
                </time>
              ) : (
                <p className="text-xs text-muted-foreground">
                  Expiry not provided
                </p>
              )}
            </div>
            <span
              className={cn(
                "col-start-2 w-fit rounded-md border px-2 py-0.5 text-[11px] font-medium tabular-nums sm:col-start-3 sm:row-start-1 sm:row-end-3",
                deadlineState.tone,
              )}
            >
              {deadlineState.label}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
