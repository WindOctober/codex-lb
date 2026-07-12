import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CodexResetForecastPage } from "@/features/codex-reset-forecast/components/codex-reset-forecast-page";
import { renderWithProviders } from "@/test/utils";

describe("CodexResetForecastPage", () => {
  it("renders probability, evidence, and historical examples", async () => {
    renderWithProviders(<CodexResetForecastPage />);

    expect((await screen.findAllByText("8%")).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("No active public signal")).toBeInTheDocument();
    expect(screen.getByText("Latest X Replies")).toBeInTheDocument();
    expect(screen.getByText("@thsottiaux")).toBeInTheDocument();
    expect(screen.getByText("May 20 Sam/Tibo reset")).toBeInTheDocument();
  });
});
