import type { z } from "zod";
import type {
	AccountSummary,
	AccountTrendsResponse,
	OauthStartResponse,
	OauthStatusResponse,
} from "@/features/accounts/schemas";
import {
	AccountSummarySchema,
	AccountTrendsResponseSchema,
	OauthCompleteResponseSchema,
	OauthStartResponseSchema,
	OauthStatusResponseSchema,
} from "@/features/accounts/schemas";
import type { ApiKey, ApiKeyCreateResponse } from "@/features/api-keys/schemas";
import {
	ApiKeyCreateResponseSchema,
	ApiKeySchema,
} from "@/features/api-keys/schemas";
import type {
	ApiKeyTrendsResponse,
	ApiKeyUsage7DayResponse,
} from "@/features/apis/schemas";
import {
	ApiKeyTrendsResponseSchema,
	ApiKeyUsage7DayResponseSchema,
} from "@/features/apis/schemas";
import type { AuthSession } from "@/features/auth/schemas";
import { AuthSessionSchema } from "@/features/auth/schemas";
import type {
	DashboardOverview,
	BridgeRuntime,
	RequestLog,
	RequestLogFilterOptions,
	RequestLogsResponse,
	OverviewTimeframe,
} from "@/features/dashboard/schemas";
import {
	DEFAULT_OVERVIEW_TIMEFRAME,
	BridgeRuntimeSchema,
	DashboardOverviewSchema,
	RequestLogFilterOptionsSchema,
	RequestLogSchema,
	RequestLogsResponseSchema,
} from "@/features/dashboard/schemas";
import type { CodexResetForecast } from "@/features/codex-reset-forecast/schemas";
import { CodexResetForecastSchema } from "@/features/codex-reset-forecast/schemas";
import type { DashboardSettings } from "@/features/settings/schemas";
import { DashboardSettingsSchema } from "@/features/settings/schemas";

// Backward-compatible type aliases
export type RequestLogEntry = RequestLog;
export type DashboardAuthSession = AuthSession;
export type OauthCompleteResponse = z.infer<typeof OauthCompleteResponseSchema>;

export type {
	AccountSummary,
	AccountTrendsResponse,
	DashboardOverview,
	BridgeRuntime,
	RequestLogsResponse,
	RequestLogFilterOptions,
	DashboardSettings,
	OauthStartResponse,
	OauthStatusResponse,
	ApiKey,
	ApiKeyCreateResponse,
	ApiKeyTrendsResponse,
	ApiKeyUsage7DayResponse,
	CodexResetForecast,
};

const BASE_TIME = new Date("2026-01-01T12:00:00Z");

function offsetIso(minutes: number): string {
	return new Date(BASE_TIME.getTime() + minutes * 60_000).toISOString();
}

export function createAccountSummary(
	overrides: Partial<AccountSummary> = {},
): AccountSummary {
	return AccountSummarySchema.parse({
		accountId: "acc_primary",
		email: "primary@example.com",
		displayName: "primary@example.com",
		planType: "plus",
		configuredPriority: 100,
		routingPriority: 100,
		kycEnabled: false,
		fastServiceTierEnabled: false,
		primaryDrainPriorityEnabled: false,
		subscriptionRenewsAt: null,
		status: "active",
		usage: {
			primaryRemainingPercent: 82,
			secondaryRemainingPercent: 67,
		},
		resetAtPrimary: offsetIso(60),
		resetAtSecondary: offsetIso(24 * 60),
		windowMinutesPrimary: 300,
		windowMinutesSecondary: 10_080,
		auth: {
			access: { expiresAt: offsetIso(30), state: null },
			refresh: { state: "stored" },
			idToken: { state: "parsed" },
		},
		...overrides,
	});
}

export function createDefaultAccounts(): AccountSummary[] {
	return [
		createAccountSummary(),
		createAccountSummary({
			accountId: "acc_secondary",
			email: "secondary@example.com",
			displayName: "secondary@example.com",
			status: "paused",
			usage: {
				primaryRemainingPercent: 45,
				secondaryRemainingPercent: 12,
			},
		}),
	];
}

function createTrendPoints(
	baseValue: number,
	count = 28,
	bucketSeconds = 6 * 3600,
): Array<{ t: string; v: number }> {
	return Array.from({ length: count }, (_, i) => ({
		t: new Date(BASE_TIME.getTime() - (count - i) * bucketSeconds * 1000).toISOString(),
		v: Math.max(0, baseValue + Math.sin(i) * baseValue * 0.3),
	}));
}

function createOverviewTimeframe(
	key: OverviewTimeframe = DEFAULT_OVERVIEW_TIMEFRAME,
) {
	switch (key) {
		case "1d":
			return {
				key,
				windowMinutes: 1_440,
				bucketSeconds: 3_600,
				bucketCount: 24,
			};
		case "30d":
			return {
				key,
				windowMinutes: 43_200,
				bucketSeconds: 86_400,
				bucketCount: 30,
			};
		case "7d":
		default:
			return {
				key: "7d" as const,
				windowMinutes: 10_080,
				bucketSeconds: 21_600,
				bucketCount: 28,
			};
	}
}

export function createDashboardOverview(
	overrides: Partial<DashboardOverview> = {},
): DashboardOverview {
	const timeframe = overrides.timeframe ?? createOverviewTimeframe();
	const accounts = overrides.accounts ?? createDefaultAccounts();
	const response = {
		lastSyncAt: offsetIso(-5),
		timeframe,
		accounts,
		summary: {
			primaryWindow: {
				remainingPercent: 63.5,
				capacityCredits: 225,
				remainingCredits: 142.875,
				resetAt: offsetIso(60),
				windowMinutes: 300,
			},
			secondaryWindow: {
				remainingPercent: 55.2,
				capacityCredits: 7560,
				remainingCredits: 4173.12,
				resetAt: offsetIso(24 * 60),
				windowMinutes: 10_080,
			},
			cost: {
				currency: "USD",
				totalUsd: 1.82,
			},
			metrics: {
				requests: 228,
				tokens: 45_000,
				cachedInputTokens: 8_200,
				errorRate: 0.028,
				errorCount: 6,
				topError: "rate_limit_exceeded",
			},
		},
		windows: {
			primary: {
				windowKey: "primary",
				windowMinutes: 300,
				accounts: accounts.map((account) => ({
					accountId: account.accountId,
					remainingPercentAvg: account.usage?.primaryRemainingPercent ?? 0,
					capacityCredits: 225,
					remainingCredits:
						((account.usage?.primaryRemainingPercent ?? 0) / 100) * 225,
				})),
			},
			secondary: {
				windowKey: "secondary",
				windowMinutes: 10_080,
				accounts: accounts.map((account) => ({
					accountId: account.accountId,
					remainingPercentAvg: account.usage?.secondaryRemainingPercent ?? 0,
					capacityCredits: 7560,
					remainingCredits:
						((account.usage?.secondaryRemainingPercent ?? 0) / 100) * 7560,
				})),
			},
		},
		trends: {
			requests: createTrendPoints(8, timeframe.bucketCount, timeframe.bucketSeconds),
			tokens: createTrendPoints(1600, timeframe.bucketCount, timeframe.bucketSeconds),
			cost: createTrendPoints(0.065, timeframe.bucketCount, timeframe.bucketSeconds),
			errorRate: createTrendPoints(0.03, timeframe.bucketCount, timeframe.bucketSeconds),
		},
		depletionPrimary: {
			risk: 0.55,
			riskLevel: "warning" as const,
			burnRate: 1.1,
			safeUsagePercent: 72.0,
			projectedExhaustionAt: null,
			secondsUntilExhaustion: null,
		},
		depletionSecondary: {
			risk: 0.65,
			riskLevel: "warning" as const,
			burnRate: 1.4,
			safeUsagePercent: 58.0,
			projectedExhaustionAt: null,
			secondsUntilExhaustion: null,
		},
		...overrides,
	};
	return DashboardOverviewSchema.parse(response);
}

export function createRequestLogEntry(
	overrides: Partial<RequestLogEntry> = {},
): RequestLogEntry {
	return RequestLogSchema.parse({
		requestedAt: offsetIso(-1),
		accountId: "acc_primary",
		apiKeyName: "Primary Key",
		requestId: "req_1",
		model: "gpt-5.1",
		transport: "http",
		serviceTier: null,
		requestedServiceTier: null,
		actualServiceTier: null,
		status: "ok",
		errorCode: null,
		errorMessage: null,
		tokens: 1800,
		cachedInputTokens: 320,
		reasoningEffort: null,
		costUsd: 0.0132,
		latencyMs: 920,
		...overrides,
	});
}

export function createDefaultRequestLogs(): RequestLogEntry[] {
	return [
		createRequestLogEntry(),
		createRequestLogEntry({
			requestId: "req_2",
			accountId: "acc_secondary",
			apiKeyName: "Secondary Key",
			status: "rate_limit",
			errorCode: "rate_limit_exceeded",
			errorMessage: "Rate limit reached",
			tokens: 0,
			cachedInputTokens: null,
			costUsd: 0,
			requestedAt: offsetIso(-2),
		}),
		createRequestLogEntry({
			requestId: "req_3",
			apiKeyName: null,
			status: "quota",
			errorCode: "insufficient_quota",
			errorMessage: "Quota exceeded",
			tokens: 0,
			cachedInputTokens: null,
			costUsd: 0,
			requestedAt: offsetIso(-3),
		}),
	];
}

export function createRequestLogsResponse(
	requests: RequestLogEntry[],
	total: number,
	hasMore: boolean,
): RequestLogsResponse {
	return RequestLogsResponseSchema.parse({
		requests,
		total,
		hasMore,
	});
}

export function createRequestLogFilterOptions(
	overrides: Partial<RequestLogFilterOptions> = {},
): RequestLogFilterOptions {
	return RequestLogFilterOptionsSchema.parse({
		accountIds: ["acc_primary", "acc_secondary"],
		modelOptions: [
			{ model: "gpt-5.1", reasoningEffort: null },
			{ model: "gpt-5.1", reasoningEffort: "high" },
		],
		statuses: ["ok", "rate_limit", "quota"],
		...overrides,
	});
}

export function createBridgeRuntime(overrides: Partial<BridgeRuntime> = {}): BridgeRuntime {
	return BridgeRuntimeSchema.parse({
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
		totalSessions: 4,
		activeSessions: 4,
		closedSessions: 0,
		inflightSessionCreations: 0,
		pendingRequests: 2,
		queuedRequests: 2,
		busySessions: 2,
		codexSessions: 1,
		promptCacheSessions: 4,
		hardSessions: 0,
		softSessions: 4,
		softShardSessions: 2,
		busyParallelSessions: 0,
		reconnectRequestedSessions: 0,
		prewarmedSessions: 0,
		capacityUsedPercent: 1.56,
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
			history: Array.from({ length: 60 }, (_, index) => ({
				bucketStart: new Date(Date.UTC(2026, 0, 1, 0, index)).toISOString(),
				latencyFirstTokenP50Ms: index % 17 === 0 ? 12_000 : 2200,
				latencyFirstTokenP95Ms: index % 17 === 0 ? 24_000 : 5200,
				successCount: 10,
				errorCount: index % 17 === 0 ? 1 : 0,
					status: index % 17 === 0 ? "warning" : "ok",
				})),
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
			byAccount: [
			{
				key: "acc_primary",
				label: "primary@example.com",
				sessions: 3,
				pendingRequests: 2,
				queuedRequests: 2,
				busySessions: 2,
				codexSessions: 1,
				reconnectRequestedSessions: 0,
			},
			{
				key: "acc_secondary",
				label: "secondary@example.com",
				sessions: 1,
				pendingRequests: 0,
				queuedRequests: 0,
				busySessions: 0,
				codexSessions: 0,
				reconnectRequestedSessions: 0,
			},
		],
		byAffinityKind: [
			{
				key: "prompt_cache",
				label: "prompt_cache",
				sessions: 4,
				pendingRequests: 2,
				queuedRequests: 2,
				busySessions: 2,
				codexSessions: 1,
				reconnectRequestedSessions: 0,
			},
		],
		byModel: [
			{
				key: "gpt-5.5",
				label: "gpt-5.5",
				sessions: 4,
				pendingRequests: 2,
				queuedRequests: 2,
				busySessions: 2,
				codexSessions: 1,
				reconnectRequestedSessions: 0,
			},
		],
		shardFamilies: [
			{
				familyHash: "sha256:abc123",
				affinityKind: "prompt_cache",
				sessions: 4,
				pendingRequests: 2,
				queuedRequests: 2,
				busySessions: 2,
				codexSessions: 1,
				shardSessions: 2,
				parallelSessions: 0,
				accounts: ["acc_primary", "acc_secondary"],
				models: ["gpt-5.5"],
			},
		],
		sessions: [
			{
				affinityKind: "prompt_cache",
				affinityKeyHash: "sha256:abc123",
				keyStrength: "soft",
				shardIndex: 0,
				parallelIndex: 0,
				accountId: "acc_primary",
				accountLabel: "primary@example.com",
				accountStatus: "active",
				model: "gpt-5.5",
				codexSession: true,
				closed: false,
				pendingRequestCount: 1,
				queuedRequestCount: 1,
				lastUsedAgoMs: 4500,
				idleTtlSeconds: 900,
				reconnectRequested: false,
				prewarmed: false,
				previousResponseCount: 1,
				turnStateAliasCount: 1,
				upstreamReconnectCount: 0,
				hasLastCompletedResponse: true,
			},
		],
		...overrides,
	});
}

export function createDashboardAuthSession(
	overrides: Partial<DashboardAuthSession> = {},
): DashboardAuthSession {
	return AuthSessionSchema.parse({
		authenticated: true,
		passwordRequired: true,
		totpRequiredOnLogin: false,
		totpConfigured: true,
		authMode: "standard",
		passwordManagementEnabled: true,
		...overrides,
	});
}

export function createDashboardSettings(
	overrides: Partial<DashboardSettings> = {},
): DashboardSettings {
	return DashboardSettingsSchema.parse({
		stickyThreadsEnabled: true,
		upstreamStreamTransport: "default",
		preferEarlierResetAccounts: false,
		routingStrategy: "high_waterline",
		openaiCacheAffinityMaxAgeSeconds: 300,
		importWithoutOverwrite: false,
		totpRequiredOnLogin: false,
		totpConfigured: true,
		apiKeyAuthEnabled: true,
		newsRefreshEnabled: false,
		scholarRefreshEnabled: false,
		...overrides,
	});
}

export function createCodexResetForecast(
	overrides: Partial<CodexResetForecast> = {},
): CodexResetForecast {
	return CodexResetForecastSchema.parse({
		generatedAt: "2026-05-28T08:00:00Z",
		horizonHours: 24,
		probability: 0.08,
		probabilityPercent: 8,
		probabilityLevel: "low",
		confidence: "medium",
		summary: "Low near-term reset odds because no active Tibo/Sam precursor is present.",
		modelVersion: "codex-reset-heuristic-2026-05-28",
		sourceNote: "Deterministic heuristic seeded from confirmed May 2026 reset examples.",
		collectionStatus: {
			refreshEnabled: true,
			refreshInProgress: false,
			lastStartedAt: "2026-05-28T07:00:00Z",
			lastCompletedAt: "2026-05-28T07:03:00Z",
			lastError: null,
			nextRefreshDueAt: "2026-05-28T08:03:00Z",
		},
		currentEvidence: [
			{
				kind: "no_active_signal",
				label: "No active public signal",
				summary: "No Tibo/Sam reset precursor has been supplied for the current 24-hour scoring window.",
				observedAt: null,
				url: null,
				source: "local heuristic",
				ageHours: null,
				score: 0,
			},
		],
		latestXItems: [
			{
				authorHandle: "@thsottiaux",
				kind: "reply",
				observedAt: "2026-05-28T07:30:00Z",
				text: "No reset signal here, just a recent Codex-related reply used to show the collector output.",
				translatedTextZh: "这里没有重置信号，只是一条用于展示采集器输出的近期 Codex 相关回复。",
					url: "https://x.com/thsottiaux/status/example-latest",
					replyTo: "@example",
					parent: {
						authorHandle: "@example",
						text: "Please reset Codex limits.",
						translatedTextZh: "请重置 Codex 限额。",
						url: "https://x.com/example/status/parent",
					},
					resetRelevance: "none",
				relevanceSummary: "近期检查到的回复，不是重置预兆。",
			},
		],
		scoreFactors: [
			{
				key: "baseline",
				label: "Historical baseline",
				value: 0.08,
				description: "Base rate from the observed May 2026 reset cadence.",
			},
			{
				key: "current_signal",
				label: "Current public signal",
				value: 0,
				description: "Highest weighted recent Tibo/Sam reset-related signal.",
			},
		],
		historicalExamples: [
			{
				label: "May 17 full reset",
				classification: "explicit announcement",
				signalAt: "2026-05-16T00:31:50Z",
				resetAt: "2026-05-16T17:51:03Z",
				leadTimeHours: 17.32,
				signalSummary: "Tibo said he would reset usage limits that evening.",
				resetSummary: "Tibo confirmed Codex usage limits had been reset across all paid plans.",
				signalUrl: "https://x.com/thsottiaux/status/2055446089957036402",
				resetUrl: "https://x.com/thsottiaux/status/2055707616605835333",
			},
			{
				label: "May 20 Sam/Tibo reset",
				classification: "short informal trigger",
				signalAt: "2026-05-19T18:31:16Z",
				resetAt: "2026-05-19T21:34:37Z",
				leadTimeHours: 3.06,
				signalSummary: "Sam posted that one like would make Tibo reset Codex rate limits.",
				resetSummary: "Tibo said he went and did the thing.",
				signalUrl: "https://x.com/sama/status/2056804900017947046",
				resetUrl: "https://x.com/thsottiaux/status/2056851041132696025",
			},
		],
		...overrides,
	});
}

export function createOauthStartResponse(
	overrides: Partial<OauthStartResponse> = {},
): OauthStartResponse {
	return OauthStartResponseSchema.parse({
		method: "browser",
		authorizationUrl: "https://auth.example.com/start",
		callbackUrl: "http://localhost:3000/api/oauth/callback",
		verificationUrl: null,
		userCode: null,
		deviceAuthId: null,
		intervalSeconds: null,
		expiresInSeconds: null,
		...overrides,
	});
}

export function createOauthStatusResponse(
	overrides: Partial<OauthStatusResponse> = {},
): OauthStatusResponse {
	return OauthStatusResponseSchema.parse({
		status: "pending",
		errorMessage: null,
		...overrides,
	});
}

export function createOauthCompleteResponse(
	overrides: Partial<OauthCompleteResponse> = {},
): OauthCompleteResponse {
	return OauthCompleteResponseSchema.parse({
		status: "ok",
		...overrides,
	});
}

export function createApiKey(overrides: Partial<ApiKey> = {}): ApiKey {
	return ApiKeySchema.parse({
		id: "key_1",
		name: "Default key",
		keyPrefix: "sk-test",
		allowedModels: ["gpt-5.1"],
		expiresAt: offsetIso(30 * 24 * 60),
		isActive: true,
		accountAssignmentScopeEnabled: false,
		assignedAccountIds: [],
		createdAt: offsetIso(-60),
		lastUsedAt: offsetIso(-5),
		limits: [
			{
				id: 1,
				limitType: "total_tokens",
				limitWindow: "weekly",
				maxValue: 1_000_000,
				currentValue: 125_000,
				modelFilter: null,
				resetAt: offsetIso(7 * 24 * 60),
			},
		],
		...overrides,
	});
}

export function createApiKeyCreateResponse(
	overrides: Partial<ApiKeyCreateResponse> = {},
): ApiKeyCreateResponse {
	return ApiKeyCreateResponseSchema.parse({
		...createApiKey(),
		key: "sk-test-generated-secret",
		...overrides,
	});
}

export function createDefaultApiKeys(): ApiKey[] {
	return [
		createApiKey(),
		createApiKey({
			id: "key_2",
			name: "Read only key",
			keyPrefix: "sk-second",
			allowedModels: ["gpt-4o-mini"],
			isActive: false,
			expiresAt: null,
			lastUsedAt: null,
			limits: [],
		}),
	];
}

function createUsageTrendPoints(
	basePercent: number,
	count = 28,
): Array<{ t: string; v: number }> {
	return Array.from({ length: count }, (_, i) => ({
		t: new Date(BASE_TIME.getTime() - (count - i) * 6 * 3600_000).toISOString(),
		v: Math.max(0, Math.min(100, basePercent + Math.sin(i) * 15)),
	}));
}

function createQuotaTimelineBuckets(count = 34) {
	return Array.from({ length: count }, (_, i) => ({
		startAt: new Date(BASE_TIME.getTime() - (count - i) * 5 * 3600_000).toISOString(),
		endAt: new Date(BASE_TIME.getTime() - (count - i - 1) * 5 * 3600_000).toISOString(),
		primaryUsedPercent: Math.max(0, Math.min(100, 30 + Math.sin(i / 2) * 24)),
		primaryUsedCredits: Math.max(0, Math.min(100, 30 + Math.sin(i / 2) * 24)),
		secondaryRemainingPercent: Math.max(0, Math.min(100, 70 - i * 0.8)),
		secondaryReset: i === 12,
		secondaryResetAt: i === 12 ? new Date(BASE_TIME.getTime() - (count - i) * 5 * 3600_000).toISOString() : null,
	}));
}

export function createAccountTrends(
	accountId: string,
	overrides: Partial<AccountTrendsResponse> = {},
): AccountTrendsResponse {
	return AccountTrendsResponseSchema.parse({
		accountId,
		primary: createUsageTrendPoints(80),
		secondary: createUsageTrendPoints(55),
		quotaTimeline: createQuotaTimelineBuckets(),
		...overrides,
	});
}

function createApiKeyTrendPoints(count = 28): Array<{ t: string; v: number }> {
	return Array.from({ length: count }, (_, i) => ({
		t: new Date(BASE_TIME.getTime() - (count - i) * 6 * 3600_000).toISOString(),
		v: 10_000 + Math.round(Math.sin(i) * 5_000),
	}));
}

export function createApiKeyTrends(
	overrides: Partial<ApiKeyTrendsResponse> = {},
): ApiKeyTrendsResponse {
	return ApiKeyTrendsResponseSchema.parse({
		keyId: "key_1",
		cost: createApiKeyTrendPoints().map((p) => ({
			...p,
			v: +(p.v * 0.001).toFixed(4),
		})),
		tokens: createApiKeyTrendPoints(),
		...overrides,
	});
}

export function createApiKeyUsage7Day(
	overrides: Partial<ApiKeyUsage7DayResponse> = {},
): ApiKeyUsage7DayResponse {
	return ApiKeyUsage7DayResponseSchema.parse({
		keyId: "key_1",
		totalTokens: 280_000,
		cachedInputTokens: 45_000,
		totalRequests: 350,
		totalCostUsd: 2.47,
		...overrides,
	});
}
