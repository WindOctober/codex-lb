import { z } from "zod";

const SourceSchema = z.object({
  title: z.string().optional().default(""),
  url: z.string().optional().default(""),
  publisher: z.string().optional().default(""),
  published_at: z.string().optional().default(""),
  source_type: z.string().optional().default(""),
}).passthrough();

export const NewsCompanySchema = z.object({
  company: z.string().optional().default(""),
  headline: z.string().optional().default(""),
  dek: z.string().optional().default(""),
  theme: z.string().optional().default(""),
  confidence: z.string().optional().default(""),
  bullets: z.array(z.string()).optional().default([]),
  sources: z.array(SourceSchema).optional().default([]),
}).passthrough();

export const NewsRumorSchema = z.object({
  headline: z.string().optional().default(""),
  summary: z.string().optional().default(""),
  display_name: z.string().optional().default(""),
  handle: z.string().optional().default(""),
  url: z.string().optional().default(""),
  posted_at: z.string().optional().default(""),
  engagement_hint: z.string().optional().default(""),
  why_it_matters: z.string().optional().default(""),
  verification_status: z.string().optional().default(""),
}).passthrough();

export const NewsXAiDynamicSchema = z.object({
  headline: z.string().optional().default(""),
  summary: z.string().optional().default(""),
  display_name: z.string().optional().default(""),
  handle: z.string().optional().default(""),
  url: z.string().optional().default(""),
  posted_at: z.string().optional().default(""),
  engagement_hint: z.string().optional().default(""),
  category: z.string().optional().default(""),
  why_it_matters: z.string().optional().default(""),
  verification_status: z.string().optional().default(""),
}).passthrough();

export const NewsXAiDynamicsSchema = z.object({
  generated_at: z.string().nullable().optional(),
  headline: z.string().optional().default(""),
  summary: z.string().optional().default(""),
  items: z.array(NewsXAiDynamicSchema).optional().default([]),
}).passthrough();

export const NewsHotspotItemSchema = z.object({
  title: z.string().optional().default(""),
  platform: z.string().optional().default(""),
  rank: z.number().optional().default(0),
  url: z.string().optional().default(""),
  why: z.string().optional().default(""),
}).passthrough();

export const NewsHotspotClusterSchema = z.object({
  title: z.string().optional().default(""),
  summary: z.string().optional().default(""),
  mood: z.string().optional().default(""),
  items: z.array(NewsHotspotItemSchema).optional().default([]),
}).passthrough();

export const NewsHotspotDigestSchema = z.object({
  generated_at: z.string().nullable().optional(),
  headline: z.string().optional().default(""),
  summary: z.string().optional().default(""),
  clusters: z.array(NewsHotspotClusterSchema).optional().default([]),
  top_items: z.array(NewsHotspotItemSchema).optional().default([]),
  source_note: z.string().optional().default(""),
}).passthrough();

export const NewsSnapshotSchema = z.object({
  status: z.string().optional().default("empty"),
  refresh_in_progress: z.boolean().optional().default(false),
  last_started_at: z.string().nullable().optional(),
  last_completed_at: z.string().nullable().optional(),
  last_error: z.string().nullable().optional(),
  last_trend_started_at: z.string().nullable().optional(),
  last_trend_completed_at: z.string().nullable().optional(),
  last_trend_error: z.string().nullable().optional(),
  trend_refresh_in_progress: z.boolean().optional().default(false),
  next_trend_refresh_due_at: z.string().nullable().optional(),
  generated_at: z.string().nullable().optional(),
  summary: z.string().optional().default(""),
  companies: z.array(NewsCompanySchema).optional().default([]),
  rumors: z.array(NewsRumorSchema).optional().default([]),
  x_ai_dynamics: NewsXAiDynamicsSchema.optional().default({ generated_at: null, headline: "", summary: "", items: [] }),
  hotspot_digest: NewsHotspotDigestSchema.optional().default({
    generated_at: null,
    headline: "",
    summary: "",
    clusters: [],
    top_items: [],
    source_note: "",
  }),
  disclaimers: z.array(z.string()).optional().default([]),
  is_stale: z.boolean().optional().default(true),
}).passthrough();

export const NewsRefreshResponseSchema = z.object({
  queued: z.boolean(),
  status: z.string(),
  refresh_in_progress: z.boolean(),
  last_completed_at: z.string().nullable().optional(),
});

export type NewsSnapshot = z.infer<typeof NewsSnapshotSchema>;
