import { describe, expect, it } from "vitest";

import {
  AccountSummarySchema,
  AccountAdditionalQuotaSchema,
  BridgeRuntimeSchema,
  DEFAULT_OVERVIEW_TIMEFRAME,
  DashboardOverviewSchema,
  DepletionSchema,
  parseOverviewTimeframe,
  RequestLogsResponseSchema,
  UsageWindowSchema,
} from "@/features/dashboard/schemas";

const ISO = "2026-01-01T00:00:00+00:00";

const EMPTY_TRENDS = {
  requests: [],
  tokens: [],
  cost: [],
  errorRate: [],
};

describe("DashboardOverviewSchema", () => {
  it("parses overview payload without request_logs", () => {
    const parsed = DashboardOverviewSchema.parse({
      lastSyncAt: ISO,
      timeframe: {
        key: "7d",
        windowMinutes: 10080,
        bucketSeconds: 21600,
        bucketCount: 28,
      },
      accounts: [],
      summary: {
        primaryWindow: {
          remainingPercent: 80,
          capacityCredits: 100,
          remainingCredits: 80,
          resetAt: ISO,
          windowMinutes: 300,
        },
        secondaryWindow: null,
        cost: {
          currency: "USD",
          totalUsd: 12.5,
        },
        metrics: {
          requests: 500,
          tokens: 2000,
          cachedInputTokens: 300,
          errorRate: 0.02,
          errorCount: 10,
          topError: null,
        },
      },
      windows: {
        primary: {
          windowKey: "primary",
          windowMinutes: 300,
          accounts: [],
        },
        secondary: null,
      },
      trends: EMPTY_TRENDS,
    });

    expect(parsed.accounts).toHaveLength(0);
  });

  it("drops legacy request_logs field from parse result", () => {
    const parsed = DashboardOverviewSchema.parse({
      lastSyncAt: ISO,
      timeframe: {
        key: "7d",
        windowMinutes: 10080,
        bucketSeconds: 21600,
        bucketCount: 28,
      },
      accounts: [],
      summary: {
        primaryWindow: {
          remainingPercent: 70,
          capacityCredits: 100,
          remainingCredits: 70,
          resetAt: ISO,
          windowMinutes: 300,
        },
        secondaryWindow: null,
        cost: {
          currency: "USD",
          totalUsd: 0,
        },
        metrics: null,
      },
      windows: {
        primary: {
          windowKey: "primary",
          windowMinutes: 300,
          accounts: [],
        },
        secondary: null,
      },
      trends: EMPTY_TRENDS,
      request_logs: [{ request_id: "legacy-row" }],
    });

    expect(parsed).not.toHaveProperty("request_logs");
  });
});

describe("RequestLogsResponseSchema", () => {
  it("requires total and hasMore metadata", () => {
    const parsed = RequestLogsResponseSchema.parse({
      requests: [],
      total: 0,
      hasMore: false,
    });

    expect(parsed.total).toBe(0);
    expect(parsed.hasMore).toBe(false);
  });

  it("rejects missing pagination metadata", () => {
    const result = RequestLogsResponseSchema.safeParse({
      requests: [],
    });

    expect(result.success).toBe(false);
  });

  it("parses request rows including apiKeyName", () => {
    const parsed = RequestLogsResponseSchema.parse({
      requests: [
        {
          requestedAt: ISO,
          accountId: "acc-1",
          planType: "plus",
          apiKeyName: "Key A",
          requestId: "req-1",
          model: "gpt-5.1",
          transport: "websocket",
          status: "ok",
          errorCode: null,
          errorMessage: null,
          tokens: 10,
          cachedInputTokens: 0,
          reasoningEffort: null,
          costUsd: 0.001,
          latencyMs: 42,
        },
      ],
      total: 1,
      hasMore: false,
    });

    expect(parsed.requests[0]?.apiKeyName).toBe("Key A");
    expect(parsed.requests[0]?.planType).toBe("plus");
    expect(parsed.requests[0]?.transport).toBe("websocket");
  });
});

describe("BridgeRuntimeSchema", () => {
  it("parses bridge runtime metrics", () => {
    const parsed = BridgeRuntimeSchema.parse({
      config: {
        enabled: true,
        maxSessions: 0,
        queueLimit: 0,
        accountModelSessionLimit: 20,
        softShardPendingLimit: 1,
        softShardMaxShards: 64,
        idleTtlSeconds: 120,
        codexIdleTtlSeconds: 900,
        promptCacheIdleTtlSeconds: 3600,
        gatewaySafeMode: false,
      },
      totalSessions: 2,
      activeSessions: 2,
      closedSessions: 0,
      inflightSessionCreations: 0,
      pendingRequests: 1,
      queuedRequests: 1,
      busySessions: 1,
      codexSessions: 1,
      promptCacheSessions: 2,
      hardSessions: 0,
      softSessions: 2,
      softShardSessions: 1,
      busyParallelSessions: 0,
      reconnectRequestedSessions: 0,
      prewarmedSessions: 0,
      capacityUsedPercent: 0.78,
      availableParallelCapacity: 39,
      freeAccountModelSessionSlots: 38,
      reclaimableIdleSessions: 1,
      accountModelSessionCapacity: 40,
      health: {
        anchorAt: "2026-01-01T00:00:00Z",
        latencyFirstTokenP50Ms: 2233,
        latencyFirstTokenP95Ms: 5200,
        latencyFirstTokenP99Ms: 8800,
        endpointPingMs: 121,
        successRatePercent: 97,
        successCount: 9779,
        requestCount: 10081,
        nextUpdateSeconds: 60,
        status: "ok",
        history: [{
          bucketStart: "2026-01-01T00:00:00Z",
          latencyFirstTokenP50Ms: 2233,
          latencyFirstTokenP95Ms: 5200,
          successCount: 10,
          errorCount: 0,
          status: "ok",
        }],
      },
      upstreamEgress: {
        mode: "auto",
        selectedRoute: "direct",
        proxyConfigured: true,
        directOk: true,
        proxyOk: false,
        directLatencyMs: 121,
        proxyLatencyMs: 4001,
        directFailureStreak: 0,
        directSuccessStreak: 2,
        lastProbeAgoMs: 1000,
        lastSwitchAgoMs: null,
      },
      byAccount: [{
        key: "acc-1",
        label: "user@example.com",
        sessions: 2,
        pendingRequests: 1,
        queuedRequests: 1,
        busySessions: 1,
        codexSessions: 1,
        reconnectRequestedSessions: 0,
      }],
      byAffinityKind: [],
      byModel: [],
      shardFamilies: [{
        familyHash: "sha256:abc",
        affinityKind: "prompt_cache",
        sessions: 2,
        pendingRequests: 1,
        queuedRequests: 1,
        busySessions: 1,
        codexSessions: 1,
        shardSessions: 1,
        parallelSessions: 0,
        accounts: ["acc-1"],
        models: ["gpt-5.5"],
      }],
      sessions: [{
        affinityKind: "prompt_cache",
        affinityKeyHash: "sha256:abc",
        keyStrength: "soft",
        shardIndex: 1,
        parallelIndex: 0,
        accountId: "acc-1",
        accountLabel: "user@example.com",
        accountStatus: "active",
        model: "gpt-5.5",
        codexSession: true,
        closed: false,
        pendingRequestCount: 1,
        queuedRequestCount: 1,
        lastUsedAgoMs: 1000,
        idleTtlSeconds: 900,
        reconnectRequested: false,
        prewarmed: false,
        previousResponseCount: 1,
        turnStateAliasCount: 1,
        upstreamReconnectCount: 0,
        hasLastCompletedResponse: true,
      }],
    });

    expect(parsed.codexSessions).toBe(1);
    expect(parsed.availableParallelCapacity).toBe(39);
    expect(parsed.health.latencyFirstTokenP50Ms).toBe(2233);
    expect(parsed.upstreamEgress.directLatencyMs).toBe(121);
    expect(parsed.sessions[0]?.shardIndex).toBe(1);
  });
});

describe("overview timeframe parsing", () => {
  it("defaults invalid values to 7d", () => {
    expect(parseOverviewTimeframe("invalid")).toBe(DEFAULT_OVERVIEW_TIMEFRAME);
    expect(parseOverviewTimeframe(null)).toBe(DEFAULT_OVERVIEW_TIMEFRAME);
  });
});

describe("UsageWindowSchema", () => {
  it("parses usage window payload", () => {
    const parsed = UsageWindowSchema.parse({
      windowKey: "secondary",
      windowMinutes: 10080,
      accounts: [
        {
          accountId: "acc-1",
          remainingPercentAvg: 42.1,
          capacityCredits: 100,
          remainingCredits: 42,
        },
      ],
    });

    expect(parsed.accounts[0]?.accountId).toBe("acc-1");
  });

  it("allows nullable remaining percent values", () => {
    const parsed = UsageWindowSchema.parse({
      windowKey: "primary",
      windowMinutes: 300,
      accounts: [
        {
          accountId: "acc-weekly-only",
          remainingPercentAvg: null,
          capacityCredits: 0,
          remainingCredits: 0,
        },
      ],
    });

    expect(parsed.accounts[0]?.remainingPercentAvg).toBeNull();
  });
});

describe("AccountSummarySchema light contract", () => {
  it("does not expose removed legacy fields", () => {
    const parsed = AccountSummarySchema.parse({
      accountId: "acc-1",
      email: "user@example.com",
      displayName: "User",
      planType: "pro",
      status: "active",
      capacity_credits_primary: 500,
      remaining_credits_primary: 300,
      capacity_credits_secondary: 2000,
      remaining_credits_secondary: 900,
      last_refresh_at: ISO,
      deactivation_reason: "manual",
    });

    expect(parsed).not.toHaveProperty("capacity_credits_primary");
    expect(parsed).not.toHaveProperty("remaining_credits_primary");
    expect(parsed).not.toHaveProperty("capacity_credits_secondary");
    expect(parsed).not.toHaveProperty("remaining_credits_secondary");
    expect(parsed).not.toHaveProperty("last_refresh_at");
    expect(parsed).not.toHaveProperty("deactivation_reason");
  });
});

describe("AccountAdditionalQuotaSchema", () => {
  it("parses valid additional quota data", () => {
    const parsed = AccountAdditionalQuotaSchema.parse({
      limitName: "requests_per_minute",
      meteredFeature: "requests",
      primaryWindow: {
        usedPercent: 45.5,
        resetAt: 1704067200,
        windowMinutes: 60,
      },
      secondaryWindow: null,
    });

    expect(parsed.limitName).toBe("requests_per_minute");
    expect(parsed.meteredFeature).toBe("requests");
    expect(parsed.primaryWindow?.usedPercent).toBe(45.5);
    expect(parsed.secondaryWindow).toBeNull();
  });

  it("allows optional window fields", () => {
    const parsed = AccountAdditionalQuotaSchema.parse({
      limitName: "tokens_per_day",
      meteredFeature: "tokens",
    });

    expect(parsed.limitName).toBe("tokens_per_day");
    expect(parsed.primaryWindow).toBeUndefined();
    expect(parsed.secondaryWindow).toBeUndefined();
  });
});

describe("DepletionSchema", () => {
  it("parses all risk levels", () => {
    const riskLevels = ["safe", "warning", "danger", "critical"] as const;

    riskLevels.forEach((level) => {
      const parsed = DepletionSchema.parse({
        risk: 0.5,
        riskLevel: level,
        burnRate: 0.1,
        safeUsagePercent: 80,
        projectedExhaustionAt: ISO,
        secondsUntilExhaustion: 86400,
      });

      expect(parsed.riskLevel).toBe(level);
    });
  });

  it("allows nullable exhaustion fields", () => {
    const parsed = DepletionSchema.parse({
      risk: 0.2,
      riskLevel: "safe",
      burnRate: 0.05,
      safeUsagePercent: 90,
      projectedExhaustionAt: null,
      secondsUntilExhaustion: null,
    });

    expect(parsed.projectedExhaustionAt).toBeNull();
    expect(parsed.secondsUntilExhaustion).toBeNull();
  });
});

describe("DashboardOverviewSchema with additional quotas", () => {
  it("parses with additionalQuotas array", () => {
    const parsed = DashboardOverviewSchema.parse({
      lastSyncAt: ISO,
      timeframe: {
        key: "7d",
        windowMinutes: 10080,
        bucketSeconds: 21600,
        bucketCount: 28,
      },
      accounts: [],
      summary: {
        primaryWindow: {
          remainingPercent: 80,
          capacityCredits: 100,
          remainingCredits: 80,
          resetAt: ISO,
          windowMinutes: 300,
        },
        secondaryWindow: null,
        cost: {
          currency: "USD",
          totalUsd: 12.5,
        },
        metrics: null,
      },
      windows: {
        primary: {
          windowKey: "primary",
          windowMinutes: 300,
          accounts: [],
        },
        secondary: null,
      },
      trends: EMPTY_TRENDS,
      additionalQuotas: [
        {
          limitName: "requests_per_minute",
          meteredFeature: "requests",
          primaryWindow: {
            usedPercent: 50,
            resetAt: 1704067200,
            windowMinutes: 60,
          },
        },
      ],
      depletionPrimary: {
        risk: 0.3,
        riskLevel: "warning",
        burnRate: 0.1,
        safeUsagePercent: 80,
      },
      depletionSecondary: {
        risk: 0.6,
        riskLevel: "danger",
        burnRate: 0.2,
        safeUsagePercent: 50,
      },
    });

    expect(parsed.additionalQuotas).toHaveLength(1);
    expect(parsed.additionalQuotas[0]?.limitName).toBe("requests_per_minute");
    expect(parsed.depletionPrimary?.riskLevel).toBe("warning");
    expect(parsed.depletionSecondary?.riskLevel).toBe("danger");
  });

  it("defaults additionalQuotas to empty array for backward compatibility", () => {
    const parsed = DashboardOverviewSchema.parse({
      lastSyncAt: ISO,
      timeframe: {
        key: "7d",
        windowMinutes: 10080,
        bucketSeconds: 21600,
        bucketCount: 28,
      },
      accounts: [],
      summary: {
        primaryWindow: {
          remainingPercent: 80,
          capacityCredits: 100,
          remainingCredits: 80,
          resetAt: ISO,
          windowMinutes: 300,
        },
        secondaryWindow: null,
        cost: {
          currency: "USD",
          totalUsd: 12.5,
        },
        metrics: null,
      },
      windows: {
        primary: {
          windowKey: "primary",
          windowMinutes: 300,
          accounts: [],
        },
        secondary: null,
      },
      trends: EMPTY_TRENDS,
    });

    expect(parsed.additionalQuotas).toEqual([]);
  });
});
