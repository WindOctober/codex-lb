import { Inbox } from "lucide-react";
import type { CSSProperties } from "react";
import { useMemo, useState } from "react";

import { isEmailLabel } from "@/components/blur-email";
import { CopyButton } from "@/components/copy-button";
import { usePrivacyStore } from "@/hooks/use-privacy";
import { EmptyState } from "@/components/empty-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { PaginationControls } from "@/features/dashboard/components/filters/pagination-controls";
import type { AccountSummary, RequestLog } from "@/features/dashboard/schemas";
import { REQUEST_STATUS_LABELS } from "@/utils/constants";
import {
  formatDateTimeInline,
  formatCompactNumber,
  formatCurrency,
  formatModelLabel,
  formatSlug,
  formatTimeLong,
} from "@/utils/formatters";

const STATUS_CLASS_MAP: Record<string, string> = {
  ok: "bg-emerald-500/15 text-emerald-700 border-emerald-500/20 hover:bg-emerald-500/20 dark:text-emerald-400",
  rate_limit: "bg-orange-500/15 text-orange-700 border-orange-500/20 hover:bg-orange-500/20 dark:text-orange-400",
  quota: "bg-red-500/15 text-red-700 border-red-500/20 hover:bg-red-500/20 dark:text-red-400",
  error: "bg-zinc-500/15 text-zinc-700 border-zinc-500/20 hover:bg-zinc-500/20 dark:text-zinc-400",
};

const TRANSPORT_LABELS: Record<string, string> = {
  http: "HTTP",
  websocket: "WS",
};

const TRANSPORT_CLASS_MAP: Record<string, string> = {
  http: "bg-slate-500/10 text-slate-700 border-slate-500/20 hover:bg-slate-500/15 dark:text-slate-300",
  websocket: "bg-sky-500/15 text-sky-700 border-sky-500/20 hover:bg-sky-500/20 dark:text-sky-300",
};

const PLAN_CLASS_MAP: Record<string, string> = {
  free: "bg-zinc-500/10 text-zinc-700 border-zinc-500/20 hover:bg-zinc-500/15 dark:text-zinc-300",
  plus: "bg-emerald-500/15 text-emerald-700 border-emerald-500/20 hover:bg-emerald-500/20 dark:text-emerald-400",
  team: "bg-sky-500/15 text-sky-700 border-sky-500/20 hover:bg-sky-500/20 dark:text-sky-300",
  pro: "bg-violet-500/15 text-violet-700 border-violet-500/20 hover:bg-violet-500/20 dark:text-violet-300",
  api_key_provider: "bg-cyan-500/15 text-cyan-700 border-cyan-500/20 hover:bg-cyan-500/20 dark:text-cyan-300",
};

type PlanRowTokens = {
  bg: string;
  border: string;
  accent: string;
  glow: string;
  sheen: string;
};

type PlanRowStyle = CSSProperties & {
  "--plan-row-bg": string;
  "--plan-row-border": string;
  "--plan-row-accent": string;
  "--plan-row-glow": string;
  "--plan-row-sheen": string;
};

const PLAN_ROW_TOKENS: Record<string, PlanRowTokens> = {
  free: {
    bg: "linear-gradient(100deg, rgba(113,113,122,0.11), rgba(113,113,122,0.045) 42%, rgba(113,113,122,0.015))",
    border: "rgba(113,113,122,0.22)",
    accent: "rgba(161,161,170,0.58)",
    glow: "rgba(113,113,122,0.42)",
    sheen: "rgba(255,255,255,0.07)",
  },
  plus: {
    bg: "linear-gradient(100deg, rgba(16,185,129,0.18), rgba(20,184,166,0.075) 44%, rgba(16,185,129,0.018))",
    border: "rgba(16,185,129,0.28)",
    accent: "rgba(52,211,153,0.82)",
    glow: "rgba(16,185,129,0.56)",
    sheen: "rgba(236,253,245,0.1)",
  },
  team: {
    bg: "linear-gradient(100deg, rgba(14,165,233,0.17), rgba(99,102,241,0.07) 46%, rgba(14,165,233,0.018))",
    border: "rgba(14,165,233,0.27)",
    accent: "rgba(56,189,248,0.78)",
    glow: "rgba(14,165,233,0.54)",
    sheen: "rgba(240,249,255,0.1)",
  },
  pro: {
    bg: "linear-gradient(100deg, rgba(139,92,246,0.22), rgba(217,70,239,0.08) 46%, rgba(139,92,246,0.022))",
    border: "rgba(167,139,250,0.34)",
    accent: "rgba(196,181,253,0.9)",
    glow: "rgba(168,85,247,0.68)",
    sheen: "rgba(245,243,255,0.12)",
  },
  enterprise: {
    bg: "linear-gradient(100deg, rgba(245,158,11,0.2), rgba(251,191,36,0.075) 44%, rgba(245,158,11,0.02))",
    border: "rgba(245,158,11,0.3)",
    accent: "rgba(251,191,36,0.84)",
    glow: "rgba(245,158,11,0.58)",
    sheen: "rgba(255,251,235,0.11)",
  },
  api_key_provider: {
    bg: "linear-gradient(100deg, rgba(6,182,212,0.18), rgba(59,130,246,0.075) 44%, rgba(6,182,212,0.02))",
    border: "rgba(6,182,212,0.29)",
    accent: "rgba(34,211,238,0.82)",
    glow: "rgba(6,182,212,0.58)",
    sheen: "rgba(236,254,255,0.11)",
  },
};

const PLAN_ROW_CLASSNAME =
  "group/request-row border-0 bg-[image:var(--plan-row-bg)] [background-size:100%_100%] transition-[background,filter,transform] duration-200 hover:-translate-y-0.5 hover:brightness-110 " +
  "[&>td]:border-y [&>td]:border-[color:var(--plan-row-border)] [&>td]:bg-transparent [&>td]:transition-[border-color] [&>td]:duration-200 " +
  "[&>td:first-child]:rounded-l-2xl [&>td:first-child]:border-l [&>td:first-child]:shadow-[inset_3px_0_0_var(--plan-row-accent)] " +
  "[&>td:last-child]:rounded-r-2xl [&>td:last-child]:border-r";

function getPlanRowStyle(planType: string | null): PlanRowStyle {
  const tokens = (planType && PLAN_ROW_TOKENS[planType]) || PLAN_ROW_TOKENS.free;
  return {
    "--plan-row-bg": tokens.bg,
    "--plan-row-border": tokens.border,
    "--plan-row-accent": tokens.accent,
    "--plan-row-glow": tokens.glow,
    "--plan-row-sheen": tokens.sheen,
  };
}

function formatPlanBadgeLabel(planType: string): string {
  if (planType === "api_key_provider") {
    return "Provider";
  }
  return formatSlug(planType);
}

export type RecentRequestsTableProps = {
  requests: RequestLog[];
  accounts: AccountSummary[];
  total: number;
  limit: number;
  offset: number;
  hasMore: boolean;
  onLimitChange: (limit: number) => void;
  onOffsetChange: (offset: number) => void;
};

export function RecentRequestsTable({
  requests,
  accounts,
  total,
  limit,
  offset,
  hasMore,
  onLimitChange,
  onOffsetChange,
}: RecentRequestsTableProps) {
  const [selectedRequest, setSelectedRequest] = useState<RequestLog | null>(null);
  const blurred = usePrivacyStore((s) => s.blurred);

  const accountLabelMap = useMemo(() => {
    const index = new Map<string, string>();
    for (const account of accounts) {
      index.set(account.accountId, account.displayName || account.email || account.accountId);
    }
    return index;
  }, [accounts]);

  /** Account IDs whose label is an email. */
  const emailLabelIds = useMemo(() => {
    const ids = new Set<string>();
    for (const account of accounts) {
      const label = account.displayName || account.email;
      if (isEmailLabel(label, account.email)) {
        ids.add(account.accountId);
      }
    }
    return ids;
  }, [accounts]);

  if (requests.length === 0) {
    return (
      <EmptyState
        icon={Inbox}
        title="No request logs"
        description="No request logs match the current filters."
      />
    );
  }

  return (
    <div className="space-y-3">
    <div className="rounded-xl border bg-card/80 p-2 shadow-sm backdrop-blur">
      <div className="relative overflow-x-auto">
        <Table className="min-w-[1240px] table-fixed border-separate border-spacing-y-2">
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead className="w-28 pl-4 text-[11px] font-medium uppercase tracking-wider text-muted-foreground/80">Time</TableHead>
              <TableHead className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground/80">Account</TableHead>
              <TableHead className="w-24 text-[11px] font-medium uppercase tracking-wider text-muted-foreground/80">Plan</TableHead>
              <TableHead className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground/80">API Key</TableHead>
              <TableHead className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground/80">Model</TableHead>
              <TableHead className="w-20 text-[11px] font-medium uppercase tracking-wider text-muted-foreground/80">Transport</TableHead>
              <TableHead className="w-24 text-[11px] font-medium uppercase tracking-wider text-muted-foreground/80">Status</TableHead>
              <TableHead className="w-24 text-right text-[11px] font-medium uppercase tracking-wider text-muted-foreground/80">Tokens</TableHead>
              <TableHead className="w-16 text-right text-[11px] font-medium uppercase tracking-wider text-muted-foreground/80">Cost</TableHead>
              <TableHead className="w-72 pr-4 text-[11px] font-medium uppercase tracking-wider text-muted-foreground/80">Error</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {requests.map((request) => {
              const time = formatTimeLong(request.requestedAt);
              const accountLabel = request.accountId ? (accountLabelMap.get(request.accountId) ?? request.accountId) : "—";
              const isEmailLabel = !!(request.accountId && emailLabelIds.has(request.accountId));
              const errorPreview = request.errorMessage || request.errorCode || "-";
              const hasError = !!(request.errorCode || request.errorMessage);
              const visibleServiceTier = request.actualServiceTier ?? request.serviceTier;
              const showRequestedTier =
                !!request.requestedServiceTier && request.requestedServiceTier !== visibleServiceTier;
              const planType = request.planType?.trim().toLowerCase() || null;
              const planLabel = planType ? formatPlanBadgeLabel(planType) : "--";

              return (
                <TableRow
                  key={request.requestId}
                  className={PLAN_ROW_CLASSNAME}
                  style={getPlanRowStyle(planType)}
                >
                  <TableCell className="pl-4 align-top">
                    <div className="leading-tight">
                      <div className="text-sm font-medium">{time.time}</div>
                      <div className="text-xs text-muted-foreground">{time.date}</div>
                    </div>
                  </TableCell>
                  <TableCell className="truncate align-top text-sm">
                    {isEmailLabel && blurred ? (
                      <span className="privacy-blur">{accountLabel}</span>
                    ) : (
                      accountLabel
                    )}
                  </TableCell>
                  <TableCell className="align-top">
                    {planType ? (
                      <Badge
                        variant="outline"
                        className={cn(
                          "max-w-20 truncate px-2",
                          PLAN_CLASS_MAP[planType] ?? PLAN_CLASS_MAP.free,
                        )}
                        title={formatSlug(planType)}
                      >
                        {planLabel}
                      </Badge>
                    ) : (
                      <span className="text-xs text-muted-foreground">--</span>
                    )}
                  </TableCell>
                  <TableCell className="truncate align-top text-xs text-muted-foreground">
                    {request.apiKeyName || "--"}
                  </TableCell>
                  <TableCell className="truncate align-top">
                    <div className="leading-tight">
                      <span className="font-mono text-xs">
                        {formatModelLabel(request.model, request.reasoningEffort, visibleServiceTier)}
                      </span>
                      {showRequestedTier ? (
                        <div className="text-[11px] text-muted-foreground">
                          Requested {request.requestedServiceTier}
                        </div>
                      ) : null}
                    </div>
                  </TableCell>
                  <TableCell className="align-top">
                    {request.transport ? (
                      <Badge
                        variant="outline"
                        className={TRANSPORT_CLASS_MAP[request.transport] ?? TRANSPORT_CLASS_MAP.http}
                      >
                        {TRANSPORT_LABELS[request.transport] ?? request.transport}
                      </Badge>
                    ) : (
                      <span className="text-xs text-muted-foreground">--</span>
                    )}
                  </TableCell>
                  <TableCell className="align-top">
                    <Badge
                      variant="outline"
                      className={STATUS_CLASS_MAP[request.status] ?? STATUS_CLASS_MAP.error}
                    >
                      {REQUEST_STATUS_LABELS[request.status] ?? request.status}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right align-top font-mono text-xs tabular-nums">
                    <div className="leading-tight">
                      <div>{formatCompactNumber(request.tokens)}</div>
                      {request.cachedInputTokens != null && request.cachedInputTokens > 0 && (
                        <div className="text-[11px] text-muted-foreground">
                          {formatCompactNumber(request.cachedInputTokens)} Cached
                        </div>
                      )}
                    </div>
                  </TableCell>
                  <TableCell className="text-right align-top font-mono text-xs tabular-nums">
                    {formatCurrency(request.costUsd)}
                  </TableCell>
                  <TableCell className="pr-4 align-top whitespace-normal">
                    {hasError ? (
                      <div className="space-y-2">
                        {request.errorCode ? (
                          <div>
                            <Badge variant="outline" className="max-w-full font-mono text-[10px]">
                              <span className="truncate">{request.errorCode}</span>
                            </Badge>
                          </div>
                        ) : null}
                        <p className="line-clamp-2 break-words text-xs leading-relaxed text-muted-foreground">
                          {errorPreview}
                        </p>
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          className="h-6 px-2 text-[11px]"
                          onClick={() => setSelectedRequest(request)}
                        >
                          View Details
                        </Button>
                      </div>
                    ) : (
                      <span className="text-xs text-muted-foreground">-</span>
                    )}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
    </div>

      <div className="flex justify-end">
        <PaginationControls
          total={total}
          limit={limit}
          offset={offset}
          hasMore={hasMore}
          onLimitChange={onLimitChange}
          onOffsetChange={onOffsetChange}
        />
      </div>

      <Dialog open={selectedRequest !== null} onOpenChange={(open) => { if (!open) setSelectedRequest(null); }}>
        <DialogContent className="max-h-[85vh] sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>Request Details</DialogTitle>
            <DialogDescription>Inspect request metadata and copy the fields you need.</DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 overflow-y-auto">
            <div className="space-y-3 rounded-md border bg-muted/30 p-4">
              <RequestDetailField
                label="Request ID"
                value={selectedRequest?.requestId ?? "—"}
                mono
                copyValue={selectedRequest?.requestId ?? ""}
                copyLabel="Copy Request ID"
                compactCopy
              />
              <div className="grid gap-3 sm:grid-cols-3">
                <RequestDetailField label="Status" value={selectedRequest ? (REQUEST_STATUS_LABELS[selectedRequest.status] ?? selectedRequest.status) : "—"} />
                <RequestDetailField label="Model" value={selectedRequest ? formatModelLabel(selectedRequest.model, selectedRequest.reasoningEffort, selectedRequest.actualServiceTier ?? selectedRequest.serviceTier) : "—"} mono />
                <RequestDetailField label="Plan" value={selectedRequest?.planType ? formatPlanBadgeLabel(selectedRequest.planType) : "—"} />
                <RequestDetailField label="Transport" value={selectedRequest?.transport ? (TRANSPORT_LABELS[selectedRequest.transport] ?? selectedRequest.transport) : "—"} />
                <RequestDetailField label="Time" value={selectedRequest ? formatDateTimeInline(selectedRequest.requestedAt) : "—"} />
                <RequestDetailField label="Error Code" value={selectedRequest?.errorCode ?? "—"} mono />
              </div>
            </div>

            <div className="space-y-2">
              <div className="flex items-center gap-2">
                <h3 className="text-sm font-medium">Full Error</h3>
                {selectedRequest?.errorMessage ? (
                  <CopyButton value={selectedRequest.errorMessage} label="Copy Error" iconOnly />
                ) : null}
              </div>
              <div className="max-h-[36vh] overflow-y-auto rounded-md bg-muted/50 p-3">
                <p className="whitespace-pre-wrap break-words font-mono text-xs leading-relaxed">
                  {selectedRequest?.errorMessage ?? selectedRequest?.errorCode ?? "No error detail recorded."}
                </p>
              </div>
            </div>
          </div>
          <DialogFooter showCloseButton />
        </DialogContent>
      </Dialog>
    </div>
  );
}

type RequestDetailFieldProps = {
  label: string;
  value: string;
  mono?: boolean;
  copyValue?: string;
  copyLabel?: string;
  compactCopy?: boolean;
};

function RequestDetailField({
  label,
  value,
  mono = false,
  copyValue,
  copyLabel = "Copy",
  compactCopy = false,
}: RequestDetailFieldProps) {
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2">
        <div className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground/80">
          {label}
        </div>
        {copyValue ? (
          <CopyButton value={copyValue} label={copyLabel} iconOnly={compactCopy} />
        ) : null}
      </div>
      <div className="flex flex-col items-start gap-2">
        <p className={`min-w-0 flex-1 break-all text-sm leading-relaxed ${mono ? "font-mono" : ""}`}>
          {value}
        </p>
      </div>
    </div>
  );
}
