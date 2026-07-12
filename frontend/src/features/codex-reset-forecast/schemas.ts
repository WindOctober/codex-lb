import { z } from "zod";

export const ResetSignalKindSchema = z.enum([
  "explicit_reset_announcement",
  "tibo_ok_request",
  "sam_trigger",
  "issue_acknowledged",
  "limit_fix",
  "confirmed_reset",
  "no_active_signal",
]);

export const CodexResetEvidenceSchema = z.object({
  kind: ResetSignalKindSchema,
  label: z.string(),
  summary: z.string(),
  observedAt: z.string().nullable(),
  url: z.string().nullable(),
  source: z.string(),
  ageHours: z.number().nullable(),
  score: z.number(),
});

export const CodexResetScoreFactorSchema = z.object({
  key: z.string(),
  label: z.string(),
  value: z.number(),
  description: z.string(),
});

export const CodexResetHistoricalExampleSchema = z.object({
  label: z.string(),
  classification: z.string(),
  signalAt: z.string(),
  resetAt: z.string(),
  leadTimeHours: z.number(),
  signalSummary: z.string(),
  resetSummary: z.string(),
  signalUrl: z.string(),
  resetUrl: z.string(),
});

export const CodexResetCollectionStatusSchema = z.object({
  refreshEnabled: z.boolean(),
  refreshInProgress: z.boolean(),
  lastStartedAt: z.string().nullable(),
  lastCompletedAt: z.string().nullable(),
  lastError: z.string().nullable(),
  nextRefreshDueAt: z.string().nullable(),
});

export const CodexResetXActivityItemSchema = z.object({
  authorHandle: z.string(),
  kind: z.enum(["post", "reply", "quote"]),
  observedAt: z.string(),
  text: z.string(),
  translatedTextZh: z.string(),
  url: z.string(),
  replyTo: z.string().nullable(),
  parent: z
    .object({
      authorHandle: z.string(),
      text: z.string(),
      translatedTextZh: z.string(),
      url: z.string().nullable(),
    })
    .nullable(),
  resetRelevance: z.enum(["none", "weak", "strong"]),
  relevanceSummary: z.string(),
});

export const CodexResetForecastSchema = z.object({
  generatedAt: z.string(),
  horizonHours: z.number(),
  probability: z.number(),
  probabilityPercent: z.number(),
  probabilityLevel: z.enum(["low", "elevated", "high"]),
  confidence: z.enum(["low", "medium", "high"]),
  summary: z.string(),
  modelVersion: z.string(),
  sourceNote: z.string(),
  collectionStatus: CodexResetCollectionStatusSchema,
  currentEvidence: z.array(CodexResetEvidenceSchema),
  latestXItems: z.array(CodexResetXActivityItemSchema),
  scoreFactors: z.array(CodexResetScoreFactorSchema),
  historicalExamples: z.array(CodexResetHistoricalExampleSchema),
});

export type CodexResetEvidence = z.infer<typeof CodexResetEvidenceSchema>;
export type CodexResetHistoricalExample = z.infer<typeof CodexResetHistoricalExampleSchema>;
export type CodexResetXActivityItem = z.infer<typeof CodexResetXActivityItemSchema>;
export type CodexResetForecast = z.infer<typeof CodexResetForecastSchema>;
