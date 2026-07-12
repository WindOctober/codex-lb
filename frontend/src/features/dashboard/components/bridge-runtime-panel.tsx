import { Activity, Clock, GitBranch, Gauge, Network, RadioTower, Wifi, Zap } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type {
  BridgeRuntime,
  BridgeRuntimeGroup,
  BridgeRuntimeHealth,
  BridgeRuntimeHealthHistoryBucket,
  BridgeRuntimeSession,
  UpstreamEgressRuntime,
} from "@/features/dashboard/schemas";
import { usePrivacyStore } from "@/hooks/use-privacy";
import { cn } from "@/lib/utils";
import { formatCompactNumber, formatPercent, formatSlug } from "@/utils/formatters";

type BridgeRuntimePanelProps = {
  runtime: BridgeRuntime | undefined;
  isLoading?: boolean;
};

type Metric = {
  label: string;
  value: string;
  meta: string;
  tone: string;
  icon: typeof Activity;
};

function maskAccountLabel(label: string | null | undefined, blurred: boolean): string {
  if (!label) {
    return "Unknown";
  }
  if (!blurred || !label.includes("@")) {
    return label;
  }
  const [name, domain] = label.split("@");
  const prefix = name ? `${name.slice(0, 2)}...` : "...";
  return `${prefix}@${domain}`;
}

function groupRows(groups: BridgeRuntimeGroup[], limit: number): BridgeRuntimeGroup[] {
  return groups.slice(0, limit);
}

function sessionRows(sessions: BridgeRuntimeSession[], limit: number): BridgeRuntimeSession[] {
  return sessions.slice(0, limit);
}

function formatAge(seconds: number): string {
  if (seconds < 60) {
    return `${Math.round(seconds)}s`;
  }
  if (seconds < 3600) {
    return `${Math.round(seconds / 60)}m`;
  }
  return `${Math.round(seconds / 3600)}h`;
}

function formatCapacity(value: number | null): string {
  return value === null ? "unlimited" : formatCompactNumber(value);
}

function formatFullNumber(value: number): string {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(value);
}

function formatMs(value: number | null): string {
  return value === null ? "n/a" : `${formatFullNumber(value)} ms`;
}

function formatOptionalAgeMs(value: number | null): string {
  if (value === null) {
    return "n/a";
  }
  return formatAge(value / 1000);
}

function formatHistoryTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function healthLabel(status: BridgeRuntimeHealth["status"]): string {
  if (status === "ok") {
    return "正常";
  }
  if (status === "warning") {
    return "延迟高";
  }
  if (status === "critical") {
    return "异常";
  }
  return "未知";
}

function healthTone(status: BridgeRuntimeHealth["status"]): string {
  if (status === "ok") {
    return "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300";
  }
  if (status === "warning") {
    return "bg-amber-500/15 text-amber-700 dark:text-amber-300";
  }
  if (status === "critical") {
    return "bg-red-500/15 text-red-700 dark:text-red-300";
  }
  return "bg-muted text-muted-foreground";
}

function historyTone(status: string): string {
  if (status === "ok") {
    return "bg-emerald-500";
  }
  if (status === "warning") {
    return "bg-amber-500";
  }
  if (status === "critical") {
    return "bg-red-500";
  }
  return "bg-muted";
}

function historyStatusLabel(status: string): string {
  if (status === "ok") {
    return "正常";
  }
  if (status === "warning") {
    return "延迟高";
  }
  if (status === "critical") {
    return "异常";
  }
  return "无数据";
}

function connectionLabel(value: boolean | null): string {
  if (value === true) {
    return "可用";
  }
  if (value === false) {
    return "探测失败";
  }
  return "未探测";
}

function connectionTone(value: boolean | null): string {
  if (value === true) {
    return "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300";
  }
  if (value === false) {
    return "bg-red-500/15 text-red-700 dark:text-red-300";
  }
  return "bg-muted text-muted-foreground";
}

function routeProbeLabel(route: string, ok: boolean | null): string {
  if (ok === true) {
    return `${route} probe ok`;
  }
  if (ok === false) {
    return `${route} probe failed`;
  }
  return `${route} probe pending`;
}

function useRefreshCountdown(seconds: number): number {
  const [deadlineMs] = useState(() => Date.now() + seconds * 1000);
  const [nowMs, setNowMs] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => {
      setNowMs(Date.now());
    }, 1000);
    return () => window.clearInterval(timer);
  }, []);

  return Math.min(seconds, Math.max(0, Math.ceil((deadlineMs - nowMs) / 1000)));
}

function metricCards(runtime: BridgeRuntime): Metric[] {
  return [
    {
      label: "Sessions",
      value:
        runtime.config.maxSessions > 0
          ? `${runtime.totalSessions}/${runtime.config.maxSessions}`
          : `${runtime.totalSessions}/unlimited`,
      meta:
        runtime.config.maxSessions > 0
          ? `${formatPercent(runtime.capacityUsedPercent)} capacity`
          : "unlimited capacity",
      tone: "bg-sky-500/10 text-sky-700 dark:text-sky-300",
      icon: Network,
    },
    {
      label: "Available",
      value: formatCapacity(runtime.availableParallelCapacity),
      meta: `${formatCapacity(runtime.freeAccountModelSessionSlots)} free + ${formatCompactNumber(runtime.reclaimableIdleSessions)} reclaimable`,
      tone: "bg-lime-500/10 text-lime-700 dark:text-lime-300",
      icon: Gauge,
    },
    {
      label: "Pending",
      value: formatCompactNumber(runtime.pendingRequests),
      meta: `${formatCompactNumber(runtime.busySessions)} busy sessions`,
      tone: "bg-amber-500/10 text-amber-700 dark:text-amber-300",
      icon: Activity,
    },
    {
      label: "Codex",
      value: formatCompactNumber(runtime.codexSessions),
      meta: `${formatCompactNumber(runtime.promptCacheSessions)} prompt-cache sessions`,
      tone: "bg-violet-500/10 text-violet-700 dark:text-violet-300",
      icon: GitBranch,
    },
    {
      label: "Shards",
      value: formatCompactNumber(runtime.softShardSessions),
      meta: `${formatCompactNumber(runtime.inflightSessionCreations)} creating`,
      tone: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
      icon: RadioTower,
    },
  ];
}

export function BridgeRuntimePanel({ runtime, isLoading = false }: BridgeRuntimePanelProps) {
  const blurred = usePrivacyStore((s) => s.blurred);

  if (isLoading && !runtime) {
    return (
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
        {Array.from({ length: 5 }).map((_, index) => (
          <div key={index} className="h-24 animate-pulse rounded-xl border bg-card/70" />
        ))}
      </div>
    );
  }

  if (!runtime) {
    return null;
  }

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
        {metricCards(runtime).map((metric) => {
          const Icon = metric.icon;
          return (
            <div key={metric.label} className="rounded-xl border bg-card p-4">
              <div className="flex items-center justify-between">
                <span className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">{metric.label}</span>
                <span className={cn("flex h-8 w-8 items-center justify-center rounded-lg", metric.tone)}>
                  <Icon className="h-4 w-4" aria-hidden="true" />
                </span>
              </div>
              <p className="mt-1 text-[1.625rem] font-semibold tracking-[-0.02em]">{metric.value}</p>
              <p className="mt-1 text-xs text-muted-foreground">{metric.meta}</p>
            </div>
          );
        })}
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <RuntimeTable title="Accounts">
          <Table className="min-w-[680px]">
            <TableHeader>
              <TableRow>
                <TableHead>Account</TableHead>
                <TableHead className="text-right">Sessions</TableHead>
                <TableHead className="text-right">Pending</TableHead>
                <TableHead className="text-right">Busy</TableHead>
                <TableHead className="text-right">Codex</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {groupRows(runtime.byAccount, 8).map((row) => (
                <TableRow key={row.key}>
                  <TableCell className="max-w-72 truncate font-medium">{maskAccountLabel(row.label, blurred)}</TableCell>
                  <TableCell className="text-right">{row.sessions}</TableCell>
                  <TableCell className="text-right">{row.pendingRequests}</TableCell>
                  <TableCell className="text-right">{row.busySessions}</TableCell>
                  <TableCell className="text-right">{row.codexSessions}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </RuntimeTable>

        <RuntimeHealthPanel
          key={`${runtime.health.anchorAt ?? "none"}:${runtime.health.endpointPingMs ?? "none"}`}
          health={runtime.health}
        />

        <UpstreamEgressPanel egress={runtime.upstreamEgress} />
      </div>

      <RuntimeTable title="Session Samples">
        <Table className="min-w-[1120px]">
          <TableHeader>
            <TableRow>
              <TableHead>Key</TableHead>
              <TableHead>Account</TableHead>
              <TableHead>Model</TableHead>
              <TableHead>Affinity</TableHead>
              <TableHead className="text-right">Pending</TableHead>
              <TableHead className="text-right">Queued</TableHead>
              <TableHead className="text-right">Idle</TableHead>
              <TableHead>Status</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {sessionRows(runtime.sessions, 12).map((session) => (
              <TableRow key={`${session.affinityKind}:${session.affinityKeyHash}:${session.shardIndex}:${session.parallelIndex}`}>
                <TableCell className="font-mono text-xs">{session.affinityKeyHash ?? "none"}</TableCell>
                <TableCell className="max-w-64 truncate">{maskAccountLabel(session.accountLabel, blurred)}</TableCell>
                <TableCell>{session.model ?? "unknown"}</TableCell>
                <TableCell>
                  <div className="flex flex-wrap gap-1">
                    <Badge variant="outline">{formatSlug(session.affinityKind)}</Badge>
                    {session.codexSession ? <Badge variant="secondary">Codex</Badge> : null}
                    {session.shardIndex > 0 ? <Badge variant="outline">Shard {session.shardIndex}</Badge> : null}
                    {session.parallelIndex > 0 ? <Badge variant="outline">Parallel {session.parallelIndex}</Badge> : null}
                  </div>
                </TableCell>
                <TableCell className="text-right">{session.pendingRequestCount}</TableCell>
                <TableCell className="text-right">{session.queuedRequestCount}</TableCell>
                <TableCell className="text-right">
                  {session.lastUsedAgoMs === null ? "n/a" : formatAge(session.lastUsedAgoMs / 1000)}
                </TableCell>
                <TableCell>
                  <div className="flex flex-wrap gap-1">
                    {session.closed ? <Badge variant="destructive">Closed</Badge> : <Badge variant="outline">Active</Badge>}
                    {session.reconnectRequested ? <Badge variant="secondary">Reconnect</Badge> : null}
                    {session.hasLastCompletedResponse ? <Badge variant="outline">Anchor</Badge> : null}
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </RuntimeTable>
    </div>
  );
}

function UpstreamEgressPanel({ egress }: { egress: UpstreamEgressRuntime }) {
  return (
    <div className="rounded-xl border bg-card/80 p-6 shadow-sm backdrop-blur">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h3 className="text-2xl font-semibold">Upstream Egress</h3>
          <div className="mt-2 flex flex-wrap gap-2 text-sm text-muted-foreground">
            <Badge variant="secondary">{egress.mode}</Badge>
            <Badge variant={egress.selectedRoute === "proxy" ? "default" : "outline"}>
              {egress.selectedRoute}
            </Badge>
            {egress.proxyConfigured ? <Badge variant="outline">proxy configured</Badge> : null}
          </div>
        </div>
        <span className="shrink-0 rounded-xl bg-muted px-3 py-2 text-sm font-semibold text-foreground">
          {egress.selectedRoute} selected
        </span>
      </div>

      <div className="mt-6 grid gap-3 sm:grid-cols-2">
        <EgressRouteMetric
          route="Direct"
          ok={egress.directOk}
          selected={egress.selectedRoute === "direct"}
          latencyMs={egress.directLatencyMs}
          streak={`${egress.directSuccessStreak} ok / ${egress.directFailureStreak} fail`}
        />
        <EgressRouteMetric
          route="Proxy"
          ok={egress.proxyOk}
          selected={egress.selectedRoute === "proxy"}
          latencyMs={egress.proxyLatencyMs}
          streak={egress.proxyConfigured ? "configured" : "not configured"}
        />
      </div>

      <div className="mt-6 grid gap-3 text-sm sm:grid-cols-2">
        <div className="rounded-lg bg-muted/35 px-4 py-3">
          <div className="text-xs font-medium uppercase tracking-wider text-muted-foreground">Last Probe</div>
          <div className="mt-2 font-mono text-lg font-semibold">{formatOptionalAgeMs(egress.lastProbeAgoMs)}</div>
        </div>
        <div className="rounded-lg bg-muted/35 px-4 py-3">
          <div className="text-xs font-medium uppercase tracking-wider text-muted-foreground">Last Switch</div>
          <div className="mt-2 font-mono text-lg font-semibold">{formatOptionalAgeMs(egress.lastSwitchAgoMs)}</div>
        </div>
      </div>
    </div>
  );
}

function EgressRouteMetric({
  route,
  ok,
  selected,
  latencyMs,
  streak,
}: {
  route: string;
  ok: boolean | null;
  selected: boolean;
  latencyMs: number | null;
  streak: string;
}) {
  return (
    <div className={cn("rounded-lg bg-muted/35 px-4 py-3", selected ? "ring-1 ring-primary/35" : "")}>
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <span className="text-sm font-semibold text-muted-foreground">{route}</span>
          {selected ? <Badge variant="outline">selected</Badge> : null}
        </div>
        <span className={cn("rounded-md px-2 py-1 text-xs font-semibold", connectionTone(ok))}>{connectionLabel(ok)}</span>
      </div>
      <div className="mt-2 font-mono text-2xl font-semibold">{formatMs(latencyMs)}</div>
      <div className="mt-1 text-xs text-muted-foreground">{routeProbeLabel(route, ok)}</div>
      <div className="mt-1 text-xs text-muted-foreground">{streak}</div>
    </div>
  );
}

function RuntimeHealthPanel({ health }: { health: BridgeRuntimeHealth }) {
  const countdown = useRefreshCountdown(health.nextUpdateSeconds);
  const successRate = health.successRatePercent === null ? "n/a" : `${health.successRatePercent.toFixed(2)}%`;
  const history = health.history.slice(-60);

  return (
    <div className="rounded-xl border bg-card/80 p-6 shadow-sm backdrop-blur">
      <div className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 items-center gap-4">
          <div className="flex h-14 w-14 shrink-0 items-center justify-center rounded-2xl bg-muted/50">
            <Network className="h-8 w-8 text-muted-foreground" aria-hidden="true" />
          </div>
          <div className="min-w-0">
            <h3 className="truncate text-2xl font-semibold">OpenAI(GPT Pro)</h3>
            <div className="mt-2 flex flex-wrap gap-2 text-sm text-muted-foreground">
              <Badge variant="secondary">OpenAI</Badge>
              <Badge variant="outline">gpt-5.5</Badge>
            </div>
          </div>
        </div>
        <span className={cn("shrink-0 rounded-xl px-3 py-2 text-sm font-semibold", healthTone(health.status))}>
          {healthLabel(health.status)}
        </span>
      </div>

      <div className="mt-6 grid gap-3 sm:grid-cols-2">
        <HealthMetric icon={Zap} label="对话延迟" value={formatMs(health.latencyFirstTokenP50Ms)} />
        <HealthMetric icon={Wifi} label="端点 PING" value={formatMs(health.endpointPingMs)} />
      </div>

      <div className="mt-6 flex items-center justify-between gap-4 text-sm">
        <span className="text-muted-foreground">官方状态</span>
        <span className="font-medium">{healthLabel(health.status)}</span>
      </div>

      <div className="mt-4 rounded-lg bg-muted/35 px-4 py-3">
        <div className="flex items-center justify-between gap-4">
          <div>
            <div className="text-sm font-semibold text-muted-foreground">可用性 (7 天)</div>
            <div className="mt-1 text-sm text-muted-foreground">
              {formatFullNumber(health.successCount)}/{formatFullNumber(health.requestCount)} 成功
            </div>
          </div>
          <div className="font-mono text-xl font-semibold text-emerald-500">{successRate}</div>
        </div>
      </div>

      <div className="mt-6">
        <div className="flex items-center justify-between gap-4 text-xs font-medium uppercase tracking-wider text-muted-foreground">
          <span>History (60pts)</span>
          <span className="inline-flex items-center gap-2">
            <Clock className="h-4 w-4" aria-hidden="true" />
            Next update in {countdown}s
          </span>
        </div>
        <div className="mt-3 grid h-8 grid-cols-[repeat(60,minmax(3px,1fr))] items-end gap-1">
          {history.map((bucket) => (
            <HistoryBucketBar key={bucket.bucketStart} bucket={bucket} />
          ))}
        </div>
        <div className="mt-2 flex justify-between text-xs uppercase tracking-wider text-muted-foreground">
          <span>Past</span>
          <span>Now</span>
        </div>
      </div>
    </div>
  );
}

function HistoryBucketBar({ bucket }: { bucket: BridgeRuntimeHealthHistoryBucket }) {
  const label = `${formatHistoryTime(bucket.bucketStart)} ${historyStatusLabel(bucket.status)}`;

  return (
    <span
      tabIndex={0}
      aria-label={label}
      className={cn(
        "group relative h-full rounded-sm outline-none ring-offset-background transition-transform hover:scale-y-110 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
        historyTone(bucket.status),
      )}
    >
      <span className="pointer-events-none absolute bottom-full left-1/2 z-30 mb-2 hidden w-56 -translate-x-1/2 rounded-md border bg-popover px-3 py-2 text-left text-xs text-popover-foreground shadow-lg group-hover:block group-focus-visible:block">
        <span className="block font-semibold">{formatHistoryTime(bucket.bucketStart)}</span>
        <span className="mt-1 block text-muted-foreground">状态: {historyStatusLabel(bucket.status)}</span>
        <span className="mt-1 block">p50: {formatMs(bucket.latencyFirstTokenP50Ms)}</span>
        <span className="block">p95: {formatMs(bucket.latencyFirstTokenP95Ms)}</span>
        <span className="mt-1 block text-muted-foreground">
          成功 {formatFullNumber(bucket.successCount)} / 错误 {formatFullNumber(bucket.errorCount)}
        </span>
      </span>
    </span>
  );
}

function HealthMetric({ icon: Icon, label, value }: { icon: typeof Activity; label: string; value: string }) {
  return (
    <div className="rounded-lg bg-muted/35 px-4 py-3">
      <div className="flex items-center gap-2 text-sm font-semibold text-muted-foreground">
        <Icon className="h-4 w-4" aria-hidden="true" />
        <span>{label}</span>
      </div>
      <div className="mt-2 font-mono text-2xl font-semibold">{value}</div>
    </div>
  );
}

function RuntimeTable({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="rounded-xl border bg-card/80 p-2 shadow-sm backdrop-blur">
      <div className="px-2 pb-1 pt-1 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
        {title}
      </div>
      <div className="overflow-x-auto">{children}</div>
    </div>
  );
}
