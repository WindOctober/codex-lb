import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AccountListItem } from "@/features/accounts/components/account-list-item";
import { createAccountSummary } from "@/test/mocks/factories";

describe("AccountListItem", () => {
  it("renders neutral quota track when secondary remaining percent is unknown", () => {
    const account = createAccountSummary({
      usage: {
        primaryRemainingPercent: 82,
        secondaryRemainingPercent: null,
      },
    });

    render(
      <AccountListItem account={account} selected={false} onSelect={vi.fn()} />,
    );

    expect(screen.getByTestId("mini-quota-track")).toHaveClass("bg-muted");
    expect(screen.queryByTestId("mini-quota-fill")).not.toBeInTheDocument();
  });

  it("renders quota fill when secondary remaining percent is available", () => {
    const account = createAccountSummary({
      usage: {
        primaryRemainingPercent: 82,
        secondaryRemainingPercent: 73,
      },
    });

    render(
      <AccountListItem account={account} selected={false} onSelect={vi.fn()} />,
    );

    expect(screen.getByTestId("mini-quota-fill")).toHaveStyle({ width: "73%" });
  });

  it("shows starred routing priority when the account is starred", () => {
    const account = createAccountSummary({
      primaryDrainPriorityEnabled: true,
    });

    render(
      <AccountListItem
        account={account}
        selected={false}
        onSelect={vi.fn()}
      />,
    );

    expect(screen.getByLabelText("Starred routing priority")).toBeInTheDocument();
  });

  it("hides starred routing priority when the account is not starred", () => {
    const account = createAccountSummary({
      primaryDrainPriorityEnabled: false,
    });

    render(
      <AccountListItem
        account={account}
        selected={false}
        onSelect={vi.fn()}
      />,
    );

    expect(
      screen.queryByLabelText("Starred routing priority"),
    ).not.toBeInTheDocument();
  });

  it("shows reset credit count when available", () => {
    const account = createAccountSummary();

    render(
      <AccountListItem
        account={account}
        selected={false}
        resetCreditCount={2}
        onSelect={vi.fn()}
      />,
    );

    expect(
      screen.getByLabelText("2 rate-limit reset credits"),
    ).toHaveTextContent("2");
  });
});
