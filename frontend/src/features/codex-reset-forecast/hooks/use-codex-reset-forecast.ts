import { useQuery } from "@tanstack/react-query";

import { getCodexResetForecast } from "@/features/codex-reset-forecast/api";

export function useCodexResetForecast() {
  return useQuery({
    queryKey: ["codex-reset-forecast"],
    queryFn: getCodexResetForecast,
    refetchInterval: 60 * 60 * 1000,
  });
}
