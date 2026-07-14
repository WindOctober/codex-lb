import { z } from "zod";

export const RoutingStrategySchema = z.enum([
  "high_waterline",
  "primary_drain",
  "capacity_weighted",
  "usage_weighted",
]);
export const UpstreamStreamTransportSchema = z.enum(["default", "auto", "http", "websocket"]);

export const DashboardSettingsSchema = z.object({
  stickyThreadsEnabled: z.boolean(),
  upstreamStreamTransport: UpstreamStreamTransportSchema,
  preferEarlierResetAccounts: z.boolean(),
  ignoreFiveHourLimit: z.boolean().default(false),
  routingStrategy: RoutingStrategySchema,
  openaiCacheAffinityMaxAgeSeconds: z.number().int().positive(),
  kycRoutingEnforcementEnabled: z.boolean().default(true),
  importWithoutOverwrite: z.boolean(),
  totpRequiredOnLogin: z.boolean(),
  totpConfigured: z.boolean(),
  apiKeyAuthEnabled: z.boolean(),
  newsRefreshEnabled: z.boolean().optional().default(false),
  scholarRefreshEnabled: z.boolean().optional().default(false),
});

export const SettingsUpdateRequestSchema = z.object({
  stickyThreadsEnabled: z.boolean(),
  upstreamStreamTransport: UpstreamStreamTransportSchema.optional(),
  preferEarlierResetAccounts: z.boolean(),
  ignoreFiveHourLimit: z.boolean().optional(),
  routingStrategy: RoutingStrategySchema.optional(),
  openaiCacheAffinityMaxAgeSeconds: z.number().int().positive().optional(),
  kycRoutingEnforcementEnabled: z.boolean().optional(),
  importWithoutOverwrite: z.boolean().optional(),
  totpRequiredOnLogin: z.boolean().optional(),
  apiKeyAuthEnabled: z.boolean().optional(),
});

export type DashboardSettings = z.infer<typeof DashboardSettingsSchema>;
export type SettingsUpdateRequest = z.infer<typeof SettingsUpdateRequestSchema>;
