import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { AppearanceSettings } from "@/features/settings/components/appearance-settings";
import { useThemeStore } from "@/hooks/use-theme";
import { useTimeFormatStore } from "@/hooks/use-time-format";

describe("AppearanceSettings", () => {
  beforeEach(() => {
    window.localStorage.clear();
    useThemeStore.setState({ preference: "light", theme: "light", initialized: true });
    useTimeFormatStore.setState({ timeFormat: "24h" });
  });

  it("exposes selected state for the 24h time-format toggle", async () => {
    const user = userEvent.setup();

    render(<AppearanceSettings />);

    const button24h = screen.getByRole("button", { name: /24h/i });

    expect(screen.queryByRole("button", { name: /12h/i })).not.toBeInTheDocument();
    expect(button24h).toHaveAttribute("aria-pressed", "true");

    await user.click(button24h);

    expect(button24h).toHaveAttribute("aria-pressed", "true");
    expect(useTimeFormatStore.getState().timeFormat).toBe("24h");
  });
});
