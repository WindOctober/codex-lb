import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AppHeader } from "@/components/layout/app-header";
import { renderWithProviders } from "@/test/utils";

describe("AppHeader", () => {
  it("hides optional News and Scholar navigation by default", () => {
    renderWithProviders(<AppHeader onLogout={vi.fn()} />);

    expect(screen.queryByRole("link", { name: "News" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Scholar" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Dashboard" })).toBeInTheDocument();
  });

  it("shows optional navigation when enabled", () => {
    renderWithProviders(<AppHeader onLogout={vi.fn()} newsEnabled scholarEnabled />);

    expect(screen.getByRole("link", { name: "News" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Scholar" })).toBeInTheDocument();
  });
});
