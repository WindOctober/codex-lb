// @vitest-environment node

import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  detectCompletedWatchedTrees,
  requestProcessCompletionNotificationPermission,
  showProcessCompletionNotification,
} from "@/features/processes/hooks/use-process-completion-watch";
import type { ProcessTreeNode } from "@/features/processes/schemas";

const toastMocks = vi.hoisted(() => ({
  success: vi.fn(),
  warning: vi.fn(),
}));

vi.mock("sonner", () => ({
  toast: toastMocks,
}));

function createTree(overrides: Partial<ProcessTreeNode> = {}): ProcessTreeNode {
  return {
    pid: 123,
    ppid: 1,
    pgid: 123,
    sid: 123,
    state: "S",
    statusLabel: "Running",
    startedAt: "2026-06-16T10:00:00Z",
    elapsedSeconds: 10,
    commandName: "codex",
    commandKind: "codex_exec",
    displayCommand: "codex exec",
    role: "Codex exec",
    taskLabel: "Translate paper",
    lifecycleStatus: "running",
    endedAt: null,
    readAt: null,
    cwd: "/workspace",
    repoPath: "/workspace",
    children: [],
    ...overrides,
  };
}

function installNotificationMock(permission: NotificationPermission) {
  type NotificationInstance = {
    title: string;
    options?: NotificationOptions;
    close: ReturnType<typeof vi.fn>;
    onclick: ((event: Event) => void) | null;
  };
  const instances: NotificationInstance[] = [];
  const requestPermission = vi.fn().mockResolvedValue(permission);
  const focus = vi.fn();
  class NotificationMock implements NotificationInstance {
    static requestPermission = requestPermission;

    static get permission() {
      return permission;
    }

    close = vi.fn();
    onclick: ((event: Event) => void) | null = null;
    title: string;
    options?: NotificationOptions;

    constructor(title: string, options?: NotificationOptions) {
      this.title = title;
      this.options = options;
      instances.push(this);
    }
  }
  vi.stubGlobal("Notification", NotificationMock);
  vi.stubGlobal("window", {
    Notification: NotificationMock,
    focus,
    history: { replaceState: vi.fn() },
  });
  return { instances, requestPermission, focus };
}

describe("process completion watch helpers", () => {
  beforeEach(() => {
    toastMocks.success.mockReset();
    toastMocks.warning.mockReset();
    vi.unstubAllGlobals();
  });

  it("detects a watched process tree transition from running to ended", () => {
    const endedTree = createTree({
      lifecycleStatus: "ended",
      statusLabel: "Ended",
      state: "X",
      endedAt: "2026-06-16T10:01:00Z",
    });
    const result = detectCompletedWatchedTrees(
      [endedTree],
      new Map([[endedTree.pid, "running"]]),
      new Set([endedTree.pid]),
    );

    expect(result.completedTrees).toEqual([endedTree]);
    expect(result.nextLifecycleByPid.get(endedTree.pid)).toBe("ended");
  });

  it("does not report already-ended watched trees on first observation", () => {
    const endedTree = createTree({ lifecycleStatus: "ended" });
    const result = detectCompletedWatchedTrees(
      [endedTree],
      new Map(),
      new Set([endedTree.pid]),
    );

    expect(result.completedTrees).toEqual([]);
  });

  it("shows a browser notification and in-app fallback for completed watched trees", () => {
    const notification = installNotificationMock("granted");
    const tree = createTree();

    showProcessCompletionNotification(tree, "Translate paper");

    expect(notification.instances).toHaveLength(1);
    expect(notification.instances[0].title).toBe("Process completed");
    expect(notification.instances[0].options?.body).toBe("Translate paper (PID 123)");
    expect(toastMocks.success).toHaveBeenCalledWith("Process completed: Translate paper");
  });

  it("returns false when browser notification permission is blocked", async () => {
    const notification = installNotificationMock("denied");

    await expect(requestProcessCompletionNotificationPermission()).resolves.toBe(false);

    expect(notification.requestPermission).not.toHaveBeenCalled();
    expect(toastMocks.warning).toHaveBeenCalledWith("Browser notification permission is blocked");
  });
});
