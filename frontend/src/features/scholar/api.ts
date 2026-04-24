import { get, post } from "@/lib/api-client";
import { ScholarRefreshResponseSchema, ScholarSnapshotSchema } from "@/features/scholar/schemas";

export function getScholarSnapshot() {
  return get("/api/scholar", ScholarSnapshotSchema);
}

export function refreshScholar() {
  return post("/api/scholar/refresh", ScholarRefreshResponseSchema);
}
