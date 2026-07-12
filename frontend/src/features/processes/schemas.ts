import { z } from "zod";

export type ProcessTreeNode = {
  pid: number;
  ppid: number;
  pgid: number;
  sid: number;
  state: string;
  statusLabel: string;
  startedAt: string | null;
  elapsedSeconds: number | null;
  commandName: string;
  commandKind: string;
  displayCommand: string;
  role: string;
  taskLabel: string | null;
  lifecycleStatus: "running" | "ended";
  endedAt: string | null;
  readAt: string | null;
  cwd: string | null;
  repoPath: string | null;
  children: ProcessTreeNode[];
};

export const ProcessTreeNodeSchema: z.ZodType<ProcessTreeNode> = z.lazy(() =>
  z.object({
    pid: z.number(),
    ppid: z.number(),
    pgid: z.number(),
    sid: z.number(),
    state: z.string(),
    statusLabel: z.string(),
    startedAt: z.string().nullable(),
    elapsedSeconds: z.number().nullable(),
    commandName: z.string(),
    commandKind: z.string(),
    displayCommand: z.string(),
    role: z.string(),
    taskLabel: z.string().nullable(),
    lifecycleStatus: z.enum(["running", "ended"]).default("running"),
    endedAt: z.string().nullable().default(null),
    readAt: z.string().nullable().default(null),
    cwd: z.string().nullable(),
    repoPath: z.string().nullable(),
    children: z.array(ProcessTreeNodeSchema),
  }),
);

export const ProcessTreesResponseSchema = z.object({
  collectedAt: z.string(),
  pollIntervalSeconds: z.number(),
  totalTrees: z.number(),
  totalProcesses: z.number(),
  trees: z.array(ProcessTreeNodeSchema),
});

export type ProcessTreesResponse = z.infer<typeof ProcessTreesResponseSchema>;

export const ProcessTreesClearUnreadResponseSchema = z.object({
  clearedCount: z.number().int().nonnegative(),
});

export type ProcessTreesClearUnreadResponse = z.infer<typeof ProcessTreesClearUnreadResponseSchema>;
