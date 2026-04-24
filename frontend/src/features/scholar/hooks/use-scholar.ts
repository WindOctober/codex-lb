import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { getScholarSnapshot, refreshScholar } from "@/features/scholar/api";

const SCHOLAR_QUERY_KEY = ["scholar"] as const;

export function useScholar() {
  const queryClient = useQueryClient();
  const scholarQuery = useQuery({
    queryKey: SCHOLAR_QUERY_KEY,
    queryFn: getScholarSnapshot,
    refetchInterval: 15 * 60_000,
  });
  const refreshMutation = useMutation({
    mutationFn: refreshScholar,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: SCHOLAR_QUERY_KEY }),
  });
  return { scholarQuery, refreshMutation };
}
