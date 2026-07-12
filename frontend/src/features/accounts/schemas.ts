import { z } from "zod";

export const UsageTrendPointSchema = z.object({
  t: z.string().datetime({ offset: true }),
  v: z.number(),
});

export const AccountUsageTrendSchema = z.object({
  primary: z.array(UsageTrendPointSchema),
  secondary: z.array(UsageTrendPointSchema),
});

export const AccountQuotaTimelineBucketSchema = z.object({
  startAt: z.string().datetime({ offset: true }),
  endAt: z.string().datetime({ offset: true }),
  primaryUsedPercent: z.number(),
  primaryUsedCredits: z.number().nullable().optional(),
  secondaryRemainingPercent: z.number().nullable().optional(),
  secondaryReset: z.boolean().default(false),
  secondaryResetAt: z.string().datetime({ offset: true }).nullable().optional(),
});

export const AccountUsageSchema = z.object({
  primaryRemainingPercent: z.number().nullable(),
  secondaryRemainingPercent: z.number().nullable(),
});

export const AccountRequestUsageSchema = z.object({
  requestCount: z.number().int().nonnegative(),
  tokens7d: z.number().int().nonnegative().optional(),
  totalTokens: z.number().int().nonnegative(),
  cachedInputTokens: z.number().int().nonnegative(),
  totalCostUsd: z.number().nonnegative(),
  estimatedTotalCost: z.number().nullable().optional(),
  estimatedTotalCostCurrency: z.string().nullable().optional(),
});

export const AccountTokenStatusSchema = z.object({
  expiresAt: z.string().datetime({ offset: true }).nullable().optional(),
  state: z.string().nullable().optional(),
});

export const AccountAuthSchema = z.object({
  access: AccountTokenStatusSchema.nullable().optional(),
  refresh: AccountTokenStatusSchema.nullable().optional(),
  idToken: AccountTokenStatusSchema.nullable().optional(),
});

export const AccountAdditionalWindowSchema = z.object({
  usedPercent: z.number(),
  resetAt: z.number().nullable().optional(),
  windowMinutes: z.number().nullable().optional(),
});

export const AccountAdditionalQuotaSchema = z.object({
  quotaKey: z.string().nullable().optional(),
  limitName: z.string(),
  meteredFeature: z.string(),
  displayLabel: z.string().nullable().optional(),
  primaryWindow: AccountAdditionalWindowSchema.nullable().optional(),
  secondaryWindow: AccountAdditionalWindowSchema.nullable().optional(),
});

export const AccountAvailabilityBreakdownSchema = z.object({
  total: z.number().int().nonnegative(),
  active: z.number().int().nonnegative(),
  rateLimited: z.number().int().nonnegative(),
  quotaLimited: z.number().int().nonnegative(),
  paused: z.number().int().nonnegative(),
  deactivated: z.number().int().nonnegative(),
});

export const AccountSummarySchema = z.object({
  accountId: z.string(),
  email: z.string(),
  displayName: z.string(),
  planType: z.string(),
  providerKind: z.string().optional(),
  storedApiKey: z.string().nullable().optional(),
  routingTier: z.string().optional(),
  routingPriority: z.number().int().optional(),
  configuredPriority: z.number().int().optional(),
  kycEnabled: z.boolean().optional(),
  fastServiceTierEnabled: z.boolean().optional(),
  primaryDrainPriorityEnabled: z.boolean().optional(),
  subscriptionRenewsAt: z.string().datetime({ offset: true }).nullable().optional(),
  groups: z.array(z.string()).optional(),
  status: z.string(),
  usage: AccountUsageSchema.nullable().optional(),
  resetAtPrimary: z.string().datetime({ offset: true }).nullable().optional(),
  resetAtSecondary: z.string().datetime({ offset: true }).nullable().optional(),
  windowMinutesPrimary: z.number().nullable().optional(),
  windowMinutesSecondary: z.number().nullable().optional(),
  requestUsage: AccountRequestUsageSchema.nullable().optional(),
  auth: AccountAuthSchema.nullable().optional(),
  additionalQuotas: z.array(AccountAdditionalQuotaSchema).default([]),
  availability: AccountAvailabilityBreakdownSchema.nullable().optional(),
});

export const AccountTrendsResponseSchema = z.object({
  accountId: z.string(),
  primary: z.array(UsageTrendPointSchema),
  secondary: z.array(UsageTrendPointSchema),
  quotaTimeline: z.array(AccountQuotaTimelineBucketSchema).default([]),
});

export const AccountsResponseSchema = z.object({
  accounts: z.array(AccountSummarySchema),
});

export const AccountFastServiceTierBulkUpdateRequestSchema = z.object({
  enabled: z.boolean(),
});

export const AccountFastServiceTierBulkUpdateResponseSchema = z.object({
  enabled: z.boolean(),
  updatedCount: z.number().int().nonnegative(),
});

export const AccountImportResponseSchema = z.object({
  accountId: z.string(),
  email: z.string(),
  planType: z.string(),
  status: z.string(),
});

export const ApiProviderCreateRequestSchema = z.object({
  name: z.string().trim().min(1),
  baseUrl: z.string().trim().min(1),
  apiKey: z.string().trim().min(1),
  priority: z.number().int().min(0).max(100000).default(100),
});

export const ApiProviderCreateResponseSchema =
  AccountImportResponseSchema.extend({
    baseUrl: z.string(),
    wireApi: z.string(),
    priority: z.number().int(),
    supportedModels: z.array(z.string()).default([]),
  });

export const AccountUpdateRequestSchema = z.object({
  configuredPriority: z.number().int().min(0).max(100000),
  kycEnabled: z.boolean().optional(),
  fastServiceTierEnabled: z.boolean().optional(),
  primaryDrainPriorityEnabled: z.boolean().optional(),
  subscriptionRenewsAt: z.string().datetime({ offset: true }).nullable().optional(),
  groups: z.array(z.string()).optional(),
});

export const AccountAvailabilityResponseSchema = z.object({
  status: z.string(),
  targetId: z.string(),
  testedCount: z.number().int().nonnegative(),
  passedCount: z.number().int().nonnegative(),
  failedCount: z.number().int().nonnegative(),
  skippedCount: z.number().int().nonnegative(),
  activeCount: z.number().int().nonnegative(),
  totalCount: z.number().int().nonnegative(),
  failedAccountIds: z.array(z.string()).default([]),
});

export const AccountRateLimitResetCreditsResponseSchema = z.object({
  accountId: z.string(),
  availableCount: z.number().int().nonnegative(),
});

export const AccountRateLimitResetConsumeRequestSchema = z.object({
  idempotencyKey: z.string().min(1).max(128).optional(),
});

export const AccountRateLimitResetConsumeResponseSchema = z.object({
  accountId: z.string(),
  outcome: z.enum([
    "reset",
    "nothing_to_reset",
    "no_credit",
    "already_redeemed",
  ]),
  availableCount: z.number().int().nonnegative().nullable().optional(),
  windowsReset: z.number().int().nonnegative(),
});

export const AccountActionResponseSchema = z.object({
  status: z.string(),
});

export const OauthStartRequestSchema = z.object({
  forceMethod: z.string().optional(),
  targetAccountId: z.string().optional(),
});

export const OauthStartResponseSchema = z.object({
  method: z.string(),
  authorizationUrl: z.string().nullable(),
  callbackUrl: z.string().nullable(),
  verificationUrl: z.string().nullable(),
  userCode: z.string().nullable(),
  deviceAuthId: z.string().nullable(),
  intervalSeconds: z.number().nullable(),
  expiresInSeconds: z.number().nullable(),
});

export const OauthStatusResponseSchema = z.object({
  status: z.string(),
  errorMessage: z.string().nullable(),
});

export const OauthCompleteRequestSchema = z.object({
  deviceAuthId: z.string().optional(),
  userCode: z.string().optional(),
});

export const OauthCompleteResponseSchema = z.object({
  status: z.string(),
});

export const ManualOauthCallbackRequestSchema = z.object({
  callbackUrl: z.string(),
});

export const ManualOauthCallbackResponseSchema = z.object({
  status: z.string(),
  errorMessage: z.string().nullable(),
});

export const RuntimeConnectAddressResponseSchema = z.object({
  connectAddress: z.string(),
});

export const OAuthStateSchema = z.object({
  status: z.enum(["idle", "starting", "pending", "success", "error"]),
  method: z.enum(["browser", "device"]).nullable(),
  authorizationUrl: z.string().nullable(),
  callbackUrl: z.string().nullable(),
  verificationUrl: z.string().nullable(),
  userCode: z.string().nullable(),
  deviceAuthId: z.string().nullable(),
  intervalSeconds: z.number().nullable(),
  expiresInSeconds: z.number().nullable(),
  errorMessage: z.string().nullable(),
});

export const ImportStateSchema = z.object({
  status: z.enum(["idle", "uploading", "success", "error"]),
  message: z.string().nullable(),
});

export type UsageTrendPoint = z.infer<typeof UsageTrendPointSchema>;
export type AccountQuotaTimelineBucket = z.infer<
  typeof AccountQuotaTimelineBucketSchema
>;
export type AccountUsageTrend = z.infer<typeof AccountUsageTrendSchema>;
export type AccountSummary = z.infer<typeof AccountSummarySchema>;
export type AccountAdditionalWindow = z.infer<
  typeof AccountAdditionalWindowSchema
>;
export type AccountAdditionalQuota = z.infer<
  typeof AccountAdditionalQuotaSchema
>;
export type AccountTrendsResponse = z.infer<typeof AccountTrendsResponseSchema>;
export type AccountFastServiceTierBulkUpdateResponse = z.infer<
  typeof AccountFastServiceTierBulkUpdateResponseSchema
>;
export type ApiProviderCreateRequest = z.infer<
  typeof ApiProviderCreateRequestSchema
>;
export type ApiProviderCreateResponse = z.infer<
  typeof ApiProviderCreateResponseSchema
>;
export type AccountAvailabilityResponse = z.infer<
  typeof AccountAvailabilityResponseSchema
>;
export type AccountRateLimitResetCreditsResponse = z.infer<
  typeof AccountRateLimitResetCreditsResponseSchema
>;
export type AccountRateLimitResetConsumeResponse = z.infer<
  typeof AccountRateLimitResetConsumeResponseSchema
>;
export type OauthStartResponse = z.infer<typeof OauthStartResponseSchema>;
export type OauthStatusResponse = z.infer<typeof OauthStatusResponseSchema>;
export type ManualOauthCallbackResponse = z.infer<
  typeof ManualOauthCallbackResponseSchema
>;
export type RuntimeConnectAddressResponse = z.infer<
  typeof RuntimeConnectAddressResponseSchema
>;
export type OAuthState = z.infer<typeof OAuthStateSchema>;
export type ImportState = z.infer<typeof ImportStateSchema>;
