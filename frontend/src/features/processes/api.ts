import { get, post } from "@/lib/api-client";

import {
  ProcessTreesClearUnreadResponseSchema,
  ProcessTreesResponseSchema,
} from "@/features/processes/schemas";

const PROCESSES_PATH = "/api/processes";

export function getProcessTrees() {
  return get(`${PROCESSES_PATH}/trees`, ProcessTreesResponseSchema);
}

export function markUnreadProcessTreesReadAndClear() {
  return post(`${PROCESSES_PATH}/trees/read-and-clear`, ProcessTreesClearUnreadResponseSchema);
}

export async function markProcessTreeRead(pid: number): Promise<void> {
  const response = await fetch(`${PROCESSES_PATH}/trees/${pid}/read`, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
    },
  });
  if (!response.ok) {
    throw new Error("Failed to mark process tree as read");
  }
}
