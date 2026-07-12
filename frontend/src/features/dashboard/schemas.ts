import { z } from "zod";

import { AccountAdditionalQuotaSchema, AccountSummarySchema, AccountUsageSchema } from "@/features/accounts/schemas";
import type { AccountSummary } from "@/features/accounts/schemas";

export { AccountAdditionalQuotaSchema, AccountSummarySchema, AccountUsageSchema };
export type { AccountSummary };
export type { AccountAdditionalQuota as AdditionalQuota } from "@/features/accounts/schemas";

export const OverviewTimeframeKeySchema = z.enum(["1d", "7d", "30d"]);
export type OverviewTimeframe = z.infer<typeof OverviewTimeframeKeySchema>;
export const DEFAULT_OVERVIEW_TIMEFRAME: OverviewTimeframe = "7d";

export function parseOverviewTimeframe(value: string | null | undefined): OverviewTimeframe {
  const parsed = OverviewTimeframeKeySchema.safeParse(value);
  return parsed.success ? parsed.data : DEFAULT_OVERVIEW_TIMEFRAME;
}

export const UsageHistoryItemSchema = z.object({
  accountId: z.string(),
  remainingPercentAvg: z.number().nullable(),
  capacityCredits: z.number(),
  remainingCredits: z.number(),
});

export const UsageWindowSchema = z.object({
  windowKey: z.string(),
  windowMinutes: z.number().nullable(),
  accounts: z.array(UsageHistoryItemSchema),
});

export const UsageSummaryWindowSchema = z.object({
  remainingPercent: z.number(),
  capacityCredits: z.number(),
  remainingCredits: z.number(),
  resetAt: z.string().datetime({ offset: true }).nullable(),
  windowMinutes: z.number().nullable(),
});

export const DashboardOverviewTimeframeSchema = z.object({
  key: OverviewTimeframeKeySchema,
  windowMinutes: z.number().int().positive(),
  bucketSeconds: z.number().int().positive(),
  bucketCount: z.number().int().positive(),
});

export const UsageCostSchema = z.object({
  currency: z.string(),
  totalUsd: z.number(),
});

export const DashboardMetricsSchema = z.object({
  requests: z.number().nullable(),
  tokens: z.number().nullable(),
  cachedInputTokens: z.number().nullable(),
  errorRate: z.number().nullable(),
  errorCount: z.number().nullable(),
  topError: z.string().nullable(),
});

export const TrendPointSchema = z.object({
  t: z.string().datetime({ offset: true }),
  v: z.number(),
});

export const MetricsTrendsSchema = z.object({
  requests: z.array(TrendPointSchema),
  tokens: z.array(TrendPointSchema),
  cost: z.array(TrendPointSchema),
  errorRate: z.array(TrendPointSchema),
});

export const DepletionSchema = z.object({
  risk: z.number(),
  riskLevel: z.enum(["safe", "warning", "danger", "critical"]),
  burnRate: z.number(),
  safeUsagePercent: z.number(),
  projectedExhaustionAt: z.string().datetime({ offset: true }).nullable().optional(),
  secondsUntilExhaustion: z.number().nullable().optional(),
});

export const DashboardOverviewSchema = z.object({
  lastSyncAt: z.string().datetime({ offset: true }).nullable(),
  timeframe: DashboardOverviewTimeframeSchema,
  accounts: z.array(AccountSummarySchema),
  groupedAccounts: z.array(AccountSummarySchema).default([]),
  summary: z.object({
    primaryWindow: UsageSummaryWindowSchema,
    secondaryWindow: UsageSummaryWindowSchema.nullable(),
    cost: UsageCostSchema,
    metrics: DashboardMetricsSchema.nullable(),
  }),
  windows: z.object({
    primary: UsageWindowSchema,
    secondary: UsageWindowSchema.nullable(),
  }),
  trends: MetricsTrendsSchema,
  additionalQuotas: z.array(AccountAdditionalQuotaSchema).default([]),
  depletionPrimary: DepletionSchema.nullable().optional(),
  depletionSecondary: DepletionSchema.nullable().optional(),
});

export const RequestLogSchema = z.object({
  requestedAt: z.string().datetime({ offset: true }),
  accountId: z.string().nullable(),
  planType: z.string().nullable().optional().default(null),
  apiKeyName: z.string().nullable(),
  requestId: z.string(),
  model: z.string(),
  transport: z.string().nullable().optional().default(null),
  serviceTier: z.string().nullable().optional().default(null),
  requestedServiceTier: z.string().nullable().optional().default(null),
  actualServiceTier: z.string().nullable().optional().default(null),
  status: z.string(),
  errorCode: z.string().nullable(),
  errorMessage: z.string().nullable(),
  tokens: z.number().nullable(),
  cachedInputTokens: z.number().nullable(),
  reasoningEffort: z.string().nullable(),
  costUsd: z.number().nullable(),
  latencyMs: z.number().nullable(),
});

export const RequestLogsResponseSchema = z.object({
  requests: z.array(RequestLogSchema),
  total: z.number().int().nonnegative(),
  hasMore: z.boolean(),
});

export const RequestLogModelOptionSchema = z.object({
  model: z.string(),
  reasoningEffort: z.string().nullable(),
});

export const RequestLogFilterOptionsSchema = z.object({
  accountIds: z.array(z.string()),
  modelOptions: z.array(RequestLogModelOptionSchema),
  statuses: z.array(z.string()),
});

export const BridgeRuntimeConfigSchema = z.object({
  enabled: z.boolean(),
  maxSessions: z.number().int().nonnegative(),
  queueLimit: z.number().int().nonnegative(),
  accountModelSessionLimit: z.number().int().nonnegative(),
  softShardPendingLimit: z.number().int().nonnegative(),
  softShardMaxShards: z.number().int().nonnegative(),
  idleTtlSeconds: z.number(),
  codexIdleTtlSeconds: z.number(),
  promptCacheIdleTtlSeconds: z.number(),
  gatewaySafeMode: z.boolean(),
});

export const BridgeRuntimeGroupSchema = z.object({
  key: z.string(),
  label: z.string(),
  sessions: z.number().int().nonnegative(),
  pendingRequests: z.number().int().nonnegative(),
  queuedRequests: z.number().int().nonnegative(),
  busySessions: z.number().int().nonnegative(),
  codexSessions: z.number().int().nonnegative(),
  reconnectRequestedSessions: z.number().int().nonnegative(),
});

export const BridgeRuntimeShardFamilySchema = z.object({
  familyHash: z.string(),
  affinityKind: z.string(),
  sessions: z.number().int().nonnegative(),
  pendingRequests: z.number().int().nonnegative(),
  queuedRequests: z.number().int().nonnegative(),
  busySessions: z.number().int().nonnegative(),
  codexSessions: z.number().int().nonnegative(),
  shardSessions: z.number().int().nonnegative(),
  parallelSessions: z.number().int().nonnegative(),
  accounts: z.array(z.string()),
  models: z.array(z.string()),
});

export const BridgeRuntimeSessionSchema = z.object({
  affinityKind: z.string(),
  affinityKeyHash: z.string().nullable(),
  keyStrength: z.string(),
  shardIndex: z.number().int().nonnegative(),
  parallelIndex: z.number().int().nonnegative(),
  accountId: z.string().nullable(),
  accountLabel: z.string().nullable(),
  accountStatus: z.string().nullable(),
  model: z.string().nullable(),
  codexSession: z.boolean(),
  closed: z.boolean(),
  pendingRequestCount: z.number().int().nonnegative(),
  queuedRequestCount: z.number().int().nonnegative(),
  lastUsedAgoMs: z.number().int().nonnegative().nullable(),
  idleTtlSeconds: z.number(),
  reconnectRequested: z.boolean(),
  prewarmed: z.boolean(),
  previousResponseCount: z.number().int().nonnegative(),
  turnStateAliasCount: z.number().int().nonnegative(),
  upstreamReconnectCount: z.number().int().nonnegative(),
  hasLastCompletedResponse: z.boolean(),
});

export const BridgeRuntimeHealthHistoryBucketSchema = z.object({
  bucketStart: z.string().datetime({ offset: true }),
  latencyFirstTokenP50Ms: z.number().int().nonnegative().nullable(),
  latencyFirstTokenP95Ms: z.number().int().nonnegative().nullable(),
  successCount: z.number().int().nonnegative(),
  errorCount: z.number().int().nonnegative(),
  status: z.enum(["ok", "warning", "critical", "empty"]),
});

export const BridgeRuntimeHealthSchema = z.object({
  anchorAt: z.string().datetime({ offset: true }).nullable(),
  latencyFirstTokenP50Ms: z.number().int().nonnegative().nullable(),
  latencyFirstTokenP95Ms: z.number().int().nonnegative().nullable(),
  latencyFirstTokenP99Ms: z.number().int().nonnegative().nullable(),
  endpointPingMs: z.number().int().nonnegative().nullable(),
  successRatePercent: z.number().nullable(),
  successCount: z.number().int().nonnegative(),
  requestCount: z.number().int().nonnegative(),
  nextUpdateSeconds: z.number().int().nonnegative(),
  status: z.enum(["ok", "warning", "critical", "unknown"]),
  history: z.array(BridgeRuntimeHealthHistoryBucketSchema),
});

export const UpstreamEgressRuntimeSchema = z.object({
  mode: z.string(),
  selectedRoute: z.string(),
  proxyConfigured: z.boolean(),
  directOk: z.boolean().nullable(),
  proxyOk: z.boolean().nullable(),
  directLatencyMs: z.number().int().nonnegative().nullable(),
  proxyLatencyMs: z.number().int().nonnegative().nullable(),
  directFailureStreak: z.number().int().nonnegative(),
  directSuccessStreak: z.number().int().nonnegative(),
  lastProbeAgoMs: z.number().int().nonnegative().nullable(),
  lastSwitchAgoMs: z.number().int().nonnegative().nullable(),
});

export const BridgeRuntimeSchema = z.object({
  config: BridgeRuntimeConfigSchema,
  totalSessions: z.number().int().nonnegative(),
  activeSessions: z.number().int().nonnegative(),
  closedSessions: z.number().int().nonnegative(),
  inflightSessionCreations: z.number().int().nonnegative(),
  pendingRequests: z.number().int().nonnegative(),
  queuedRequests: z.number().int().nonnegative(),
  busySessions: z.number().int().nonnegative(),
  codexSessions: z.number().int().nonnegative(),
  promptCacheSessions: z.number().int().nonnegative(),
  hardSessions: z.number().int().nonnegative(),
  softSessions: z.number().int().nonnegative(),
  softShardSessions: z.number().int().nonnegative(),
  busyParallelSessions: z.number().int().nonnegative(),
  reconnectRequestedSessions: z.number().int().nonnegative(),
  prewarmedSessions: z.number().int().nonnegative(),
  capacityUsedPercent: z.number(),
  availableParallelCapacity: z.number().int().nonnegative().nullable(),
  freeAccountModelSessionSlots: z.number().int().nonnegative().nullable(),
  reclaimableIdleSessions: z.number().int().nonnegative(),
  accountModelSessionCapacity: z.number().int().nonnegative().nullable(),
  health: BridgeRuntimeHealthSchema,
  upstreamEgress: UpstreamEgressRuntimeSchema,
  byAccount: z.array(BridgeRuntimeGroupSchema),
  byAffinityKind: z.array(BridgeRuntimeGroupSchema),
  byModel: z.array(BridgeRuntimeGroupSchema),
  shardFamilies: z.array(BridgeRuntimeShardFamilySchema),
  sessions: z.array(BridgeRuntimeSessionSchema),
});

export const FilterStateSchema = z.object({
  search: z.string(),
  timeframe: z.enum(["all", "1h", "24h", "7d"]),
  accountIds: z.array(z.string()),
  modelOptions: z.array(z.string()),
  statuses: z.array(z.string()),
  limit: z.number().int().positive(),
  offset: z.number().int().nonnegative(),
});

export type DashboardMetrics = z.infer<typeof DashboardMetricsSchema>;
export type DashboardOverview = z.infer<typeof DashboardOverviewSchema>;
export type DashboardOverviewTimeframe = z.infer<typeof DashboardOverviewTimeframeSchema>;
export type TrendPoint = z.infer<typeof TrendPointSchema>;
export type MetricsTrends = z.infer<typeof MetricsTrendsSchema>;
export type UsageWindow = z.infer<typeof UsageWindowSchema>;
export type RequestLog = z.infer<typeof RequestLogSchema>;
export type RequestLogsResponse = z.infer<typeof RequestLogsResponseSchema>;
export type RequestLogFilterOptions = z.infer<typeof RequestLogFilterOptionsSchema>;
export type BridgeRuntime = z.infer<typeof BridgeRuntimeSchema>;
export type BridgeRuntimeHealth = z.infer<typeof BridgeRuntimeHealthSchema>;
export type BridgeRuntimeHealthHistoryBucket = z.infer<typeof BridgeRuntimeHealthHistoryBucketSchema>;
export type UpstreamEgressRuntime = z.infer<typeof UpstreamEgressRuntimeSchema>;
export type BridgeRuntimeGroup = z.infer<typeof BridgeRuntimeGroupSchema>;
export type BridgeRuntimeShardFamily = z.infer<typeof BridgeRuntimeShardFamilySchema>;
export type BridgeRuntimeSession = z.infer<typeof BridgeRuntimeSessionSchema>;
export type FilterState = z.infer<typeof FilterStateSchema>;
export type Depletion = z.infer<typeof DepletionSchema>;
