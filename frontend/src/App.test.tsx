import { screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "@/App";
import { useAuthStore } from "@/features/auth/hooks/use-auth";
import { renderWithProviders } from "@/test/utils";

function setAuthenticatedSession(): void {
  useAuthStore.setState({
    initialized: true,
    loading: false,
    passwordRequired: false,
    authenticated: true,
    totpRequiredOnLogin: false,
    bootstrapRequired: false,
    bootstrapTokenConfigured: false,
    authMode: "standard",
    passwordManagementEnabled: true,
    passwordSessionActive: false,
    error: null,
    refreshSession: vi.fn().mockResolvedValue(undefined),
  });
}

describe("App optional dashboard surfaces", () => {
  beforeEach(() => {
    window.history.pushState({}, "", "/");
    setAuthenticatedSession();
  });

  it("redirects direct News visits when News refresh is disabled", async () => {
    window.history.pushState({}, "", "/news");

    renderWithProviders(<App />);

    await waitFor(() => expect(window.location.pathname).toBe("/dashboard"));
    expect(screen.queryByTitle("News")).not.toBeInTheDocument();
  });
});
