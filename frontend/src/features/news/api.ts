import { get, post } from "@/lib/api-client";
import { NewsRefreshResponseSchema, NewsSnapshotSchema } from "@/features/news/schemas";

export function getNewsSnapshot() {
  return get("/api/news", NewsSnapshotSchema);
}

export function refreshNews() {
  return post("/api/news/refresh", NewsRefreshResponseSchema);
}
