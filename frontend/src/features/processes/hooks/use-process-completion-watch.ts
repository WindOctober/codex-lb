import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import type { ProcessTreeNode } from "@/features/processes/schemas";

const PROCESS_COMPLETION_WATCH_STORAGE_KEY = "codex-lb-process-completion-watch:v1";

type ProcessLifecycleStatus = ProcessTreeNode["lifecycleStatus"];

function loadWatchedPids(): Set<number> {
  if (typeof window === "undefined") {
    return new Set();
  }
  try {
    const raw = window.localStorage.getItem(PROCESS_COMPLETION_WATCH_STORAGE_KEY);
    if (!raw) {
      return new Set();
    }
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      return new Set();
    }
    return new Set(parsed.filter((value): value is number => Number.isInteger(value) && value > 0));
  } catch {
    return new Set();
  }
}

function saveWatchedPids(pids: Set<number>): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.setItem(
      PROCESS_COMPLETION_WATCH_STORAGE_KEY,
      JSON.stringify([...pids].sort((left, right) => left - right)),
    );
  } catch {
    // localStorage may be unavailable in private browsing or restricted contexts.
  }
}

function browserNotificationsSupported(): boolean {
  return typeof window !== "undefined" && "Notification" in window;
}

export async function requestProcessCompletionNotificationPermission(): Promise<boolean> {
  if (!browserNotificationsSupported()) {
    toast.warning("Browser notifications are not supported");
    return false;
  }
  if (Notification.permission === "granted") {
    return true;
  }
  if (Notification.permission === "denied") {
    toast.warning("Browser notification permission is blocked");
    return false;
  }
  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    toast.warning("Browser notification permission was not granted");
    return false;
  }
  return true;
}

export function showProcessCompletionNotification(tree: ProcessTreeNode, title: string): void {
  const body = `${title} (PID ${tree.pid})`;
  if (browserNotificationsSupported() && Notification.permission === "granted") {
    try {
      const notification = new Notification("Process completed", {
        body,
        tag: `codex-lb-process-${tree.pid}`,
      });
      notification.onclick = () => {
        window.focus();
        notification.close();
      };
    } catch {
      // Keep the in-app fallback below.
    }
  }
  toast.success(`Process completed: ${title}`);
}

export function detectCompletedWatchedTrees(
  trees: ProcessTreeNode[],
  previousLifecycleByPid: Map<number, ProcessLifecycleStatus>,
  watchedPids: Set<number>,
): { completedTrees: ProcessTreeNode[]; nextLifecycleByPid: Map<number, ProcessLifecycleStatus> } {
  const nextLifecycleByPid = new Map<number, ProcessLifecycleStatus>();
  const completedTrees: ProcessTreeNode[] = [];

  for (const tree of trees) {
    nextLifecycleByPid.set(tree.pid, tree.lifecycleStatus);
    if (
      watchedPids.has(tree.pid)
      && previousLifecycleByPid.get(tree.pid) === "running"
      && tree.lifecycleStatus === "ended"
    ) {
      completedTrees.push(tree);
    }
  }

  return { completedTrees, nextLifecycleByPid };
}

export function useProcessCompletionWatch(
  trees: ProcessTreeNode[],
  getTreeTitle: (tree: ProcessTreeNode) => string,
) {
  const [watchedPids, setWatchedPids] = useState<Set<number>>(() => loadWatchedPids());
  const watchedPidsRef = useRef(watchedPids);
  const previousLifecycleByPidRef = useRef<Map<number, ProcessLifecycleStatus>>(new Map());

  useEffect(() => {
    watchedPidsRef.current = watchedPids;
    saveWatchedPids(watchedPids);
  }, [watchedPids]);

  useEffect(() => {
    const previousLifecycleByPid = previousLifecycleByPidRef.current;
    const { completedTrees, nextLifecycleByPid } = detectCompletedWatchedTrees(
      trees,
      previousLifecycleByPid,
      watchedPidsRef.current,
    );
    previousLifecycleByPidRef.current = nextLifecycleByPid;
    if (completedTrees.length === 0) {
      return;
    }

    queueMicrotask(() => setWatchedPids((current) => {
      const next = new Set(current);
      for (const tree of completedTrees) {
        next.delete(tree.pid);
      }
      return next;
    }));

    for (const tree of completedTrees) {
      showProcessCompletionNotification(tree, getTreeTitle(tree));
    }
  }, [getTreeTitle, trees]);

  const toggleWatch = useCallback(async (tree: ProcessTreeNode) => {
    if (watchedPidsRef.current.has(tree.pid)) {
      setWatchedPids((current) => {
        const next = new Set(current);
        next.delete(tree.pid);
        return next;
      });
      toast.success("Process watch removed");
      return;
    }
    if (tree.lifecycleStatus === "ended") {
      toast.warning("Ended processes cannot be watched");
      return;
    }
    const allowed = await requestProcessCompletionNotificationPermission();
    if (!allowed) {
      return;
    }
    setWatchedPids((current) => {
      const next = new Set(current);
      next.add(tree.pid);
      return next;
    });
    toast.success(`Watching process: ${getTreeTitle(tree)}`);
  }, [getTreeTitle]);

  const isWatched = useCallback((pid: number) => watchedPids.has(pid), [watchedPids]);

  return {
    isWatched,
    toggleWatch,
    watchedCount: watchedPids.size,
  };
}
