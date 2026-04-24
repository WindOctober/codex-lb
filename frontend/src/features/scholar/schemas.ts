import { z } from "zod";

const PaperSchema = z.object({
  title: z.string().optional().default(""),
  authors: z.string().optional().default(""),
  venue: z.string().optional().default(""),
  published_at: z.string().optional().default(""),
  url: z.string().optional().default(""),
  source_type: z.string().optional().default(""),
  summary: z.string().optional().default(""),
  technical_points: z.array(z.string()).optional().default([]),
  why_it_matters: z.string().optional().default(""),
}).passthrough();

export const ScholarTopicSchema = z.object({
  id: z.string().optional().default(""),
  label: z.string().optional().default(""),
  why_track: z.string().optional().default(""),
  published: z.array(PaperSchema).optional().default([]),
  preprints: z.array(PaperSchema).optional().default([]),
}).passthrough();

export const ScholarSnapshotSchema = z.object({
  status: z.string().optional().default("empty"),
  refresh_in_progress: z.boolean().optional().default(false),
  last_started_at: z.string().nullable().optional(),
  last_completed_at: z.string().nullable().optional(),
  last_error: z.string().nullable().optional(),
  generated_at: z.string().nullable().optional(),
  summary: z.string().optional().default(""),
  topics: z.array(ScholarTopicSchema).optional().default([]),
  disclaimers: z.array(z.string()).optional().default([]),
  is_stale: z.boolean().optional().default(true),
}).passthrough();

export const ScholarRefreshResponseSchema = z.object({
  queued: z.boolean(),
  status: z.string(),
  refresh_in_progress: z.boolean(),
  last_completed_at: z.string().nullable().optional(),
});

export type ScholarSnapshot = z.infer<typeof ScholarSnapshotSchema>;
