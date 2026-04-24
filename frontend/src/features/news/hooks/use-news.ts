import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { getNewsSnapshot, refreshNews } from "@/features/news/api";

const NEWS_QUERY_KEY = ["news"] as const;

export function useNews() {
  const queryClient = useQueryClient();
  const newsQuery = useQuery({
    queryKey: NEWS_QUERY_KEY,
    queryFn: getNewsSnapshot,
    refetchInterval: 5 * 60_000,
  });
  const refreshMutation = useMutation({
    mutationFn: refreshNews,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: NEWS_QUERY_KEY }),
  });
  return { newsQuery, refreshMutation };
}
