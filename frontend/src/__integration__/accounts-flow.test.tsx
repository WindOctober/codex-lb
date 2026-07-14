import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import App from "@/App";
import { renderWithProviders } from "@/test/utils";

describe("accounts flow integration", () => {
  it("updates the five-hour routing override", async () => {
    const user = userEvent.setup({ delay: null });

    window.history.pushState({}, "", "/accounts");
    renderWithProviders(<App />);

    expect(
      await screen.findByRole("heading", { name: "Accounts" }),
    ).toBeInTheDocument();
    const toggle = await screen.findByRole("switch", {
      name: "Ignore five-hour limit",
    });
    expect(toggle).not.toBeChecked();

    await user.click(toggle);

    await waitFor(() => expect(toggle).toBeChecked());
    expect(
      await screen.findByText("Account routing settings updated"),
    ).toBeInTheDocument();
  });

  it("supports account selection and pause/resume actions", async () => {
    const user = userEvent.setup({ delay: null });

    window.history.pushState({}, "", "/accounts");
    renderWithProviders(<App />);

    expect(
      await screen.findByRole("heading", { name: "Accounts" }),
    ).toBeInTheDocument();
    expect(
      (await screen.findAllByText("primary@example.com")).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByText("secondary@example.com")).not.toBeInTheDocument();
    expect(
      screen.getAllByRole("combobox").some((element) => element.textContent?.includes("Active")),
    ).toBe(true);

    await user.click(screen.getAllByText("primary@example.com")[0]);
    expect(await screen.findByText("Token Status")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Re-authenticate" }),
    ).toBeInTheDocument();

    const resumeButton = screen.queryByRole("button", { name: "Resume" });
    if (resumeButton) {
      await user.click(resumeButton);
      await waitFor(() => {
        expect(
          screen.getByRole("button", { name: "Pause" }),
        ).toBeInTheDocument();
      });
    } else {
      await user.click(screen.getByRole("button", { name: "Pause" }));
      await waitFor(() => {
        expect(
          screen.getByRole("button", { name: "Resume" }),
        ).toBeInTheDocument();
      });
    }
  });

  it("confirms before consuming a rate-limit reset credit", async () => {
    const user = userEvent.setup({ delay: null });

    window.history.pushState({}, "", "/accounts");
    renderWithProviders(<App />);

    expect(
      await screen.findByRole("heading", { name: "Accounts" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("1 available")).toBeInTheDocument();
    expect(
      await screen.findByText("Full reset (Weekly + 5 hr)"),
    ).toBeInTheDocument();
    expect(screen.getByText(/^Expires /)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Use reset" }));
    expect(
      await screen.findByRole("heading", { name: "Use rate-limit reset" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Consume one reset credit for primary@example\.com/),
    ).toBeInTheDocument();

    const resetButtons = screen.getAllByRole("button", { name: "Use reset" });
    await user.click(resetButtons[resetButtons.length - 1]);

    await waitFor(() => {
      expect(screen.getByText("0 available")).toBeInTheDocument();
      expect(
        screen.queryByText("Full reset (Weekly + 5 hr)"),
      ).not.toBeInTheDocument();
    });
  });
});
