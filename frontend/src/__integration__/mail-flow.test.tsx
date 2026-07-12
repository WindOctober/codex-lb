import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import App from "@/App";
import { renderWithProviders } from "@/test/utils";

describe("mail flow integration", () => {
  it("renders focused messages as a standalone mail route", async () => {
    window.history.pushState({}, "", "/mail");
    renderWithProviders(<App />);

    expect(await screen.findByRole("heading", { name: "Mail" })).toBeInTheDocument();
    expect(
      await screen.findAllByText("OpenAI Support <support@openai.com>"),
    ).not.toHaveLength(0);
    expect(screen.getAllByText(/last synced/).length).toBeGreaterThan(0);
    expect(screen.getAllByText("OpenAI").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Case update")).not.toHaveLength(0);
  });
});
