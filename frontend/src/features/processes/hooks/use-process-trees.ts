import { useQuery } from "@tanstack/react-query";

import { getProcessTrees } from "@/features/processes/api";

export const PROCESS_TREES_QUERY_KEY = ["processes", "trees"] as const;

export function useProcessTrees() {
  return useQuery({
    queryKey: PROCESS_TREES_QUERY_KEY,
    queryFn: getProcessTrees,
    refetchInterval: 10_000,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
    staleTime: 5_000,
  });
}
