import { useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Bell,
  BellOff,
  CheckCheck,
  ChevronDown,
  ChevronRight,
  Clock3,
  FolderGit2,
  GitBranch,
  Play,
  RefreshCw,
  Server,
  Terminal,
} from "lucide-react";
import { toast } from "sonner";

import { AlertMessage } from "@/components/alert-message";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  markProcessTreeRead,
  markUnreadProcessTreesReadAndClear,
} from "@/features/processes/api";
import { PROCESS_TREES_QUERY_KEY, useProcessTrees } from "@/features/processes/hooks/use-process-trees";
import { useProcessCompletionWatch } from "@/features/processes/hooks/use-process-completion-watch";
import type { ProcessTreeNode, ProcessTreesResponse } from "@/features/processes/schemas";
import { cn } from "@/lib/utils";
import { getErrorMessageOrNull } from "@/utils/errors";

type StatTileProps = {
  label: string;
  value: string;
  tone: "cyan" | "green" | "amber" | "violet";
};

const TONE_CLASSES: Record<StatTileProps["tone"], string> = {
  cyan: "border-cyan-500/20 bg-cyan-500/10 text-cyan-600 dark:text-cyan-300",
  green: "border-emerald-500/20 bg-emerald-500/10 text-emerald-600 dark:text-emerald-300",
  amber: "border-amber-500/20 bg-amber-500/10 text-amber-600 dark:text-amber-300",
  violet: "border-violet-500/20 bg-violet-500/10 text-violet-600 dark:text-violet-300",
};

function countExecTrees(trees: ProcessTreeNode[]): number {
  return trees.filter((tree) => tree.commandKind === "codex_exec").length;
}

function countAppServers(trees: ProcessTreeNode[]): number {
  return trees.filter((tree) => tree.commandKind === "codex_app_server").length;
}

function formatElapsed(seconds: number | null): string {
  if (seconds === null) return "unknown";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainingSeconds = seconds % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${remainingSeconds}s`;
  return `${remainingSeconds}s`;
}

function formatDateTime(value: string | null): string {
  if (!value) return "unknown";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function countTreeProcesses(node: ProcessTreeNode): number {
  return 1 + node.children.reduce((total, child) => total + countTreeProcesses(child), 0);
}

function findFirstNode(
  node: ProcessTreeNode,
  predicate: (candidate: ProcessTreeNode) => boolean,
): ProcessTreeNode | null {
  if (predicate(node)) {
    return node;
  }
  for (const child of node.children) {
    const match = findFirstNode(child, predicate);
    if (match) {
      return match;
    }
  }
  return null;
}

function compactPathLabel(path: string | null): string | null {
  if (!path) {
    return null;
  }
  const parts = path.split("/").filter(Boolean);
  if (parts.length === 0) {
    return path;
  }
  return parts.at(-1) ?? path;
}

function pathDepth(path: string): number {
  return path.split("/").filter(Boolean).length;
}

function collectTreePaths(node: ProcessTreeNode, paths: string[] = []): string[] {
  const path = node.repoPath ?? node.cwd;
  if (path) {
    paths.push(path);
  }
  for (const child of node.children) {
    collectTreePaths(child, paths);
  }
  return paths;
}

function representativePathForTree(tree: ProcessTreeNode): string | null {
  const rootPath = tree.repoPath ?? tree.cwd;
  const candidates = collectTreePaths(tree)
    .filter((path) => path !== rootPath)
    .filter((path) => compactPathLabel(path) !== "kejingyu");
  if (candidates.length === 0) {
    return rootPath;
  }

  const counts = new Map<string, { count: number; depth: number; firstIndex: number }>();
  candidates.forEach((path, index) => {
    const existing = counts.get(path);
    if (existing) {
      existing.count += 1;
      return;
    }
    counts.set(path, { count: 1, depth: pathDepth(path), firstIndex: index });
  });

  return [...counts.entries()].sort(([, left], [, right]) => {
    if (left.count !== right.count) return right.count - left.count;
    if (left.depth !== right.depth) return right.depth - left.depth;
    return left.firstIndex - right.firstIndex;
  })[0]?.[0] ?? rootPath;
}

function sessionTitleForTree(tree: ProcessTreeNode): string {
  if (tree.commandKind !== "codex_exec" && tree.commandKind !== "codex_app_server" && tree.taskLabel) {
    return tree.taskLabel;
  }
  const execNode = findFirstNode(
    tree,
    (node) => node.commandKind === "codex_exec" && Boolean(node.taskLabel),
  );
  if (execNode?.taskLabel) {
    return execNode.taskLabel;
  }
  if (tree.commandKind === "codex_app_server") {
    return "Vscode Codex APP";
  }
  return tree.taskLabel ?? tree.role;
}

function sessionSubtitleForTree(tree: ProcessTreeNode): string {
  const path = compactPathLabel(representativePathForTree(tree));
  if (tree.commandKind === "codex_app_server") {
    return path ? `Workspace · ${path}` : "VS Code interface";
  }
  if (path) {
    return path;
  }
  return `PID ${tree.pid}`;
}

function isAppServerTree(tree: ProcessTreeNode): boolean {
  return tree.commandKind === "codex_app_server";
}

function processTreeSortKey(tree: ProcessTreeNode): string {
  return `${tree.lifecycleStatus === "ended" ? "2" : "0"}:${isAppServerTree(tree) ? "1" : "0"}:${sessionTitleForTree(tree)}:${tree.pid}`;
}

function isUnreadEndedNode(node: ProcessTreeNode): boolean {
  return node.lifecycleStatus === "ended" && node.readAt === null;
}

function statusDotClass(node: ProcessTreeNode): string {
  if (isUnreadEndedNode(node)) return "bg-amber-400 shadow-[0_0_10px_rgba(251,191,36,0.50)]";
  if (node.lifecycleStatus === "ended") return "bg-zinc-500";
  if (node.state === "R") return "bg-emerald-400 shadow-[0_0_10px_rgba(52,211,153,0.45)]";
  if (node.state === "D") return "bg-amber-500";
  if (node.state === "T" || node.state === "Z") return "bg-rose-500";
  return "bg-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.35)]";
}

function nodeIconClass(node: ProcessTreeNode): string {
  if (isUnreadEndedNode(node)) return "border-amber-400/50 bg-amber-500/15 text-amber-600 dark:text-amber-300";
  if (node.lifecycleStatus === "ended") return "border-zinc-500/20 bg-zinc-500/10 text-zinc-400";
  if (node.state === "R") return "border-emerald-400/50 bg-emerald-500/15 text-emerald-600 dark:text-emerald-300";
  if (node.state === "D") return "border-amber-500/25 bg-amber-500/10 text-amber-600 dark:text-amber-300";
  if (node.state === "T" || node.state === "Z") return "border-rose-500/25 bg-rose-500/10 text-rose-600 dark:text-rose-300";
  if (node.commandKind === "codex_exec") return "border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-300";
  return "border-border bg-background text-muted-foreground";
}

function selectedTreeIconClass(tree: ProcessTreeNode): string {
  if (isUnreadEndedNode(tree)) return "border-amber-400/60 bg-amber-500/20 text-amber-600 dark:text-amber-300";
  if (tree.lifecycleStatus === "ended") return "border-zinc-500/30 bg-zinc-500/10 text-zinc-400";
  return "border-emerald-400/60 bg-emerald-500/20 text-emerald-600 dark:text-emerald-300";
}

function treeButtonClass(tree: ProcessTreeNode, selected: boolean): string {
  if (tree.lifecycleStatus === "ended") {
    if (isUnreadEndedNode(tree)) {
      return selected
        ? "border-amber-400/75 bg-amber-500/20 text-foreground shadow-[0_0_0_1px_rgba(251,191,36,0.20),0_14px_30px_rgba(245,158,11,0.12)]"
        : "border-amber-400/45 bg-amber-500/10 text-foreground shadow-[0_0_0_1px_rgba(251,191,36,0.10)] hover:border-amber-400/70 hover:bg-amber-500/15";
    }
    return selected
      ? "border-zinc-500/50 bg-zinc-500/15 text-foreground shadow-[var(--shadow-xs)]"
      : "border-zinc-500/25 bg-zinc-500/5 text-muted-foreground opacity-80 hover:border-zinc-500/40 hover:bg-zinc-500/10 hover:text-foreground";
  }
  if (selected) {
    return "border-emerald-400/70 bg-emerald-500/15 text-foreground shadow-[0_0_0_1px_rgba(52,211,153,0.18),0_14px_30px_rgba(16,185,129,0.10)]";
  }
  return "border-emerald-500/25 bg-emerald-500/5 text-muted-foreground hover:border-emerald-400/50 hover:bg-emerald-500/10 hover:text-foreground";
}

function StatTile({ label, value, tone }: StatTileProps) {
  return (
    <div className={cn("rounded-lg border px-4 py-3", TONE_CLASSES[tone])}>
      <div className="text-[11px] font-medium uppercase tracking-wider opacity-80">{label}</div>
      <div className="mt-1 text-2xl font-semibold tracking-tight text-foreground">{value}</div>
    </div>
  );
}

function ProcessIcon({ kind }: { kind: string }) {
  if (kind === "codex_app_server") return <Server className="h-4 w-4" aria-hidden="true" />;
  if (kind === "codex_exec") return <Terminal className="h-4 w-4" aria-hidden="true" />;
  if (kind === "mcp_server") return <GitBranch className="h-4 w-4" aria-hidden="true" />;
  return <Activity className="h-4 w-4" aria-hidden="true" />;
}

function ProcessNode({ node, depth = 0 }: { node: ProcessTreeNode; depth?: number }) {
  const path = node.repoPath ?? node.cwd;
  return (
    <div className="relative">
      {depth > 0 ? (
        <div
          className="absolute bottom-0 top-0 w-px bg-border"
          style={{ left: `${Math.max(0, depth - 1) * 24 + 10}px` }}
        />
      ) : null}
      <div
        className={cn(
          "relative grid min-h-16 grid-cols-[minmax(0,1fr)_auto] items-start gap-3 border-b border-border/60 px-4 py-3 last:border-b-0",
          depth > 0 && "bg-muted/20",
        )}
        style={{ paddingLeft: `${16 + depth * 24}px` }}
      >
        {depth > 0 ? (
          <div
            className="absolute h-px w-4 bg-border"
            style={{ left: `${(depth - 1) * 24 + 10}px`, top: "32px" }}
          />
        ) : null}
        <div className="min-w-0">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <span
              className={cn(
                "inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md border",
                nodeIconClass(node),
              )}
            >
              <ProcessIcon kind={node.commandKind} />
            </span>
            <span className="min-w-0 truncate text-sm font-semibold">
              {node.taskLabel ?? node.role}
            </span>
            <Badge variant="secondary" className="rounded-md px-1.5 py-0 text-[10px]">
              PID {node.pid}
            </Badge>
            <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
              <span className={cn("h-1.5 w-1.5 rounded-full", statusDotClass(node))} aria-hidden="true" />
              {isUnreadEndedNode(node) ? "Unread ended" : node.statusLabel}
            </span>
          </div>
          <div className="mt-2 grid gap-1 text-xs text-muted-foreground">
            <div className="min-w-0 truncate font-mono text-[11px] text-foreground/80">
              {node.displayCommand}
            </div>
            {path ? (
              <div className="flex min-w-0 items-center gap-1.5">
                <FolderGit2 className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                <span className="truncate font-mono text-[11px]">{path}</span>
              </div>
            ) : null}
          </div>
        </div>
        <div className="grid min-w-24 justify-items-end gap-1 text-right text-xs text-muted-foreground">
          <span className="inline-flex items-center gap-1">
            <Clock3 className="h-3.5 w-3.5" aria-hidden="true" />
            {formatElapsed(node.elapsedSeconds)}
          </span>
          <span>PPID {node.ppid}</span>
        </div>
      </div>
      {node.children.map((child) => (
        <ProcessNode key={child.pid} node={child} depth={depth + 1} />
      ))}
    </div>
  );
}

type ProcessTreePanelProps = {
  tree: ProcessTreeNode;
  watched: boolean;
  onToggleWatch: (tree: ProcessTreeNode) => void;
};

function ProcessTreePanel({ tree, watched, onToggleWatch }: ProcessTreePanelProps) {
  const title = sessionTitleForTree(tree);
  const subtitle = sessionSubtitleForTree(tree);
  const canWatch = tree.lifecycleStatus !== "ended";
  return (
    <section className="overflow-hidden rounded-lg border border-border bg-card shadow-[var(--shadow-xs)]">
      <div className="flex flex-col gap-3 border-b border-border bg-muted/30 px-4 py-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <Play className="h-4 w-4 text-primary" aria-hidden="true" />
            <h2 className="min-w-0 truncate text-base font-semibold tracking-tight">
              {title}
            </h2>
          </div>
        <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
            <span>{subtitle}</span>
            {tree.lifecycleStatus === "ended" && tree.endedAt ? (
              <span>Ended {formatDateTime(tree.endedAt)}</span>
            ) : null}
            <span>Started {formatDateTime(tree.startedAt)}</span>
            <span>PGID {tree.pgid}</span>
            <span>SID {tree.sid}</span>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button
            type="button"
            size="sm"
            variant={watched ? "secondary" : "outline"}
            className="h-8 gap-1.5 px-2.5 text-xs"
            disabled={!canWatch}
            onClick={() => onToggleWatch(tree)}
            title={watched ? "Stop watching process completion" : "Watch process completion"}
          >
            {watched ? (
              <BellOff className="h-3.5 w-3.5" aria-hidden="true" />
            ) : (
              <Bell className="h-3.5 w-3.5" aria-hidden="true" />
            )}
            {watched ? "Unwatch" : "Watch"}
          </Button>
          <Badge variant="outline" className="w-fit rounded-md">
            {tree.children.length} children
          </Badge>
        </div>
      </div>
      <ProcessNode node={tree} />
    </section>
  );
}

type ProcessTreeSelectorProps = {
  selectedPid: number | null;
  trees: ProcessTreeNode[];
  onSelect: (pid: number) => void;
};

type ProcessTreeButtonProps = {
  selected: boolean;
  tree: ProcessTreeNode;
  onSelect: (pid: number) => void;
  compact?: boolean;
  titleOverride?: string;
};

function ProcessTreeButton({ selected, tree, onSelect, compact = false, titleOverride }: ProcessTreeButtonProps) {
  const title = titleOverride ?? sessionTitleForTree(tree);
  const subtitle = sessionSubtitleForTree(tree);
  const processCount = countTreeProcesses(tree);
  return (
    <button
      type="button"
      title={title}
      onClick={() => onSelect(tree.pid)}
      className={cn(
        "grid shrink-0 grid-cols-[auto_minmax(0,1fr)] gap-3 rounded-md border text-left transition-colors lg:w-full",
        compact ? "min-h-16 w-60 px-2.5 py-2.5 lg:w-full" : "min-h-20 w-64 px-3 py-3",
        treeButtonClass(tree, selected),
      )}
      aria-pressed={selected}
    >
      <span
        className={cn(
          "mt-0.5 inline-flex items-center justify-center rounded-md border",
          compact ? "h-7 w-7" : "h-8 w-8",
          selected
            ? selectedTreeIconClass(tree)
            : nodeIconClass(tree),
        )}
      >
        <ProcessIcon kind={tree.commandKind} />
      </span>
      <span className="min-w-0">
        <span className="block truncate text-sm font-semibold">{title}</span>
        <span className="mt-1 block truncate text-xs">{subtitle}</span>
        <span className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px]">
          <span>PID {tree.pid}</span>
          {tree.lifecycleStatus === "ended" ? <span>{isUnreadEndedNode(tree) ? "Unread" : "Read"}</span> : null}
          <span>{processCount} processes</span>
          <span>{formatElapsed(tree.elapsedSeconds)}</span>
        </span>
      </span>
    </button>
  );
}

function ProcessTreeSelector({ selectedPid, trees, onSelect }: ProcessTreeSelectorProps) {
  const [appServersExpanded, setAppServersExpanded] = useState(false);
  const appServerTrees = trees.filter(isAppServerTree);
  const primaryTrees = trees.filter((tree) => !isAppServerTree(tree));
  const selectedAppServer = appServerTrees.some((tree) => tree.pid === selectedPid);
  const showAppServers = appServersExpanded || selectedAppServer;
  return (
    <section className="rounded-lg border border-border bg-card p-2 shadow-[var(--shadow-xs)]">
      <div className="mb-2 flex items-center justify-between px-2 pt-1">
        <h2 className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
          Sessions
        </h2>
        <Badge variant="outline" className="rounded-md px-1.5 py-0 text-[10px]">
          {trees.length}
        </Badge>
      </div>
      <div className="flex gap-2 overflow-x-auto pb-1 lg:max-h-[calc(100vh-19rem)] lg:flex-col lg:overflow-y-auto lg:overflow-x-hidden lg:pb-0">
        {primaryTrees.map((tree) => (
          <ProcessTreeButton
            key={tree.pid}
            selected={tree.pid === selectedPid}
            tree={tree}
            onSelect={onSelect}
          />
        ))}
        {appServerTrees.length > 0 ? (
          <div className="w-64 shrink-0 lg:w-full">
            <button
              type="button"
              onClick={() => setAppServersExpanded((expanded) => !expanded)}
              className={cn(
                "flex min-h-14 w-full items-center gap-3 rounded-md border px-3 py-3 text-left transition-colors",
                selectedAppServer
                  ? "border-primary/50 bg-primary/10 text-foreground shadow-[var(--shadow-xs)]"
                  : "border-border/70 bg-background/60 text-muted-foreground hover:border-primary/30 hover:bg-accent/60 hover:text-foreground",
              )}
              aria-expanded={showAppServers}
            >
              <span className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-border bg-muted/40">
                {showAppServers ? (
                  <ChevronDown className="h-4 w-4" aria-hidden="true" />
                ) : (
                  <ChevronRight className="h-4 w-4" aria-hidden="true" />
                )}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-semibold">Vscode Codex APP</span>
                <span className="mt-1 block truncate text-xs">
                  {appServerTrees.length} workspaces
                </span>
              </span>
              <Badge variant="outline" className="rounded-md px-1.5 py-0 text-[10px]">
                {appServerTrees.length}
              </Badge>
            </button>
            {showAppServers ? (
              <div className="mt-2 grid gap-2 border-l border-border/70 pl-3">
                {appServerTrees.map((tree) => (
                  <ProcessTreeButton
                    key={tree.pid}
                    compact
                    selected={tree.pid === selectedPid}
                    titleOverride={compactPathLabel(representativePathForTree(tree)) ?? sessionTitleForTree(tree)}
                    tree={tree}
                    onSelect={onSelect}
                  />
                ))}
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
    </section>
  );
}

function ProcessesSkeleton() {
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {Array.from({ length: 4 }).map((_, index) => (
          <Skeleton key={index} className="h-24 rounded-lg" />
        ))}
      </div>
      <Skeleton className="h-80 rounded-lg" />
    </div>
  );
}

function SummaryTiles({ data }: { data: ProcessTreesResponse }) {
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <StatTile label="Trees" value={String(data.totalTrees)} tone="cyan" />
      <StatTile label="Processes" value={String(data.totalProcesses)} tone="green" />
      <StatTile label="Exec roots" value={String(countExecTrees(data.trees))} tone="amber" />
      <StatTile label="App servers" value={String(countAppServers(data.trees))} tone="violet" />
    </div>
  );
}

export function ProcessesPage() {
  const queryClient = useQueryClient();
  const processTreesQuery = useProcessTrees();
  const data = processTreesQuery.data;
  const isRefreshing = processTreesQuery.isFetching;
  const sortedTrees = useMemo(
    () => [...(data?.trees ?? [])].sort((left, right) => processTreeSortKey(left).localeCompare(processTreeSortKey(right))),
    [data?.trees],
  );
  const unreadEndedCount = useMemo(
    () => sortedTrees.filter(isUnreadEndedNode).length,
    [sortedTrees],
  );
  const [selectedPid, setSelectedPid] = useState<number | null>(null);
  const selectedTree = useMemo(
    () => sortedTrees.find((tree) => tree.pid === selectedPid) ?? sortedTrees[0] ?? null,
    [selectedPid, sortedTrees],
  );
  const { isWatched, toggleWatch, watchedCount } = useProcessCompletionWatch(sortedTrees, sessionTitleForTree);
  const clearUnreadMutation = useMutation({
    mutationFn: markUnreadProcessTreesReadAndClear,
    onSuccess: async (response) => {
      if (response.clearedCount > 0) {
        toast.success(
          response.clearedCount === 1
            ? "Cleared 1 unread process"
            : `Cleared ${response.clearedCount} unread processes`,
        );
      }
      if (selectedTree !== null && isUnreadEndedNode(selectedTree)) {
        setSelectedPid(null);
      }
      await queryClient.invalidateQueries({ queryKey: PROCESS_TREES_QUERY_KEY });
    },
    onError: (error) => {
      toast.error(error instanceof Error ? error.message : "Failed to clear unread processes");
    },
  });
  const errorMessage =
    getErrorMessageOrNull(processTreesQuery.error) ||
    getErrorMessageOrNull(clearUnreadMutation.error);

  useEffect(() => {
    if (selectedTree?.lifecycleStatus !== "ended" || selectedTree.readAt !== null) {
      return;
    }
    void markProcessTreeRead(selectedTree.pid).then(() => {
      void queryClient.invalidateQueries({ queryKey: PROCESS_TREES_QUERY_KEY });
    }).catch(() => undefined);
  }, [queryClient, selectedTree?.lifecycleStatus, selectedTree?.pid, selectedTree?.readAt]);

  return (
    <div className="animate-fade-in-up space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Processes</h1>
          <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-muted-foreground">
            <span>Poll {data?.pollIntervalSeconds ?? 10}s</span>
            <span>Updated {data ? formatDateTime(data.collectedAt) : "pending"}</span>
            {watchedCount > 0 ? <span>{watchedCount} watched</span> : null}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="h-8 gap-1.5 px-2.5 text-xs"
            disabled={unreadEndedCount === 0 || clearUnreadMutation.isPending}
            onClick={() => {
              void clearUnreadMutation.mutateAsync();
            }}
            title="Mark unread ended processes read and clear them"
          >
            <CheckCheck className="h-3.5 w-3.5" aria-hidden="true" />
            Read + clear
            {unreadEndedCount > 0 ? (
              <Badge variant="secondary" className="ml-0.5 rounded-md px-1.5 py-0 text-[10px]">
                {unreadEndedCount}
              </Badge>
            ) : null}
          </Button>
          <button
            type="button"
            onClick={() => {
              void queryClient.invalidateQueries({ queryKey: PROCESS_TREES_QUERY_KEY });
            }}
            disabled={isRefreshing}
            className="inline-flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground disabled:pointer-events-none disabled:opacity-50"
            title="Refresh processes"
          >
            <RefreshCw className={`h-4 w-4${isRefreshing ? " animate-spin" : ""}`} />
          </button>
        </div>
      </div>

      {errorMessage ? <AlertMessage variant="error">{errorMessage}</AlertMessage> : null}

      {!data ? (
        <ProcessesSkeleton />
      ) : (
        <>
          <SummaryTiles data={data} />
          {sortedTrees.length === 0 ? (
            <section className="rounded-lg border border-dashed border-border bg-muted/20 px-6 py-12 text-center">
              <Terminal className="mx-auto h-8 w-8 text-muted-foreground" aria-hidden="true" />
              <h2 className="mt-3 text-sm font-semibold">No Codex process trees</h2>
            </section>
          ) : (
            <div className="grid gap-4 lg:grid-cols-[20rem_minmax(0,1fr)]">
              <ProcessTreeSelector
                selectedPid={selectedTree?.pid ?? null}
                trees={sortedTrees}
                onSelect={setSelectedPid}
              />
              {selectedTree ? (
                <ProcessTreePanel
                  key={selectedTree.pid}
                  tree={selectedTree}
                  watched={isWatched(selectedTree.pid)}
                  onToggleWatch={(tree) => {
                    void toggleWatch(tree);
                  }}
                />
              ) : null}
            </div>
          )}
        </>
      )}
    </div>
  );
}
