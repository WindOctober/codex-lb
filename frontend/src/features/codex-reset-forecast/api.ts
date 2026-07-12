import { get } from "@/lib/api-client";
import { CodexResetForecastSchema } from "@/features/codex-reset-forecast/schemas";

const FORECAST_PATH = "/api/codex-reset/forecast";

export function getCodexResetForecast() {
  return get(FORECAST_PATH, CodexResetForecastSchema);
}
