import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { AccountCard } from "@/features/dashboard/components/account-card";
import { usePrivacyStore } from "@/hooks/use-privacy";
import { createAccountSummary } from "@/test/mocks/factories";

afterEach(() => {
  act(() => {
    usePrivacyStore.setState({ blurred: false });
  });
});

describe("AccountCard", () => {
  it("renders both 5h and weekly quota bars for regular accounts", () => {
    const account = createAccountSummary();
    render(<AccountCard account={account} />);

    expect(screen.getByText("Plus")).toBeInTheDocument();
    expect(screen.getByText("5h")).toBeInTheDocument();
    expect(screen.getByText("Weekly")).toBeInTheDocument();
  });

  it("hides 5h quota bar for weekly-only accounts", () => {
    const account = createAccountSummary({
      planType: "free",
      usage: {
        primaryRemainingPercent: null,
        secondaryRemainingPercent: 76,
      },
      windowMinutesPrimary: null,
      windowMinutesSecondary: 10_080,
    });

    render(<AccountCard account={account} />);

    expect(screen.getByText("Free")).toBeInTheDocument();
    expect(screen.queryByText("5h")).not.toBeInTheDocument();
    expect(screen.getByText("Weekly")).toBeInTheDocument();
  });

  it("renders request usage for API-key providers instead of quota bars", () => {
    const account = createAccountSummary({
      accountId: "provider_123",
      email: "DuckCoding (jp.duckcoding.com)",
      displayName: "DuckCoding (jp.duckcoding.com)",
      planType: "api_key_provider",
      providerKind: "api_key",
      requestUsage: {
        requestCount: 12,
        tokens7d: 1234,
        totalTokens: 56789,
        cachedInputTokens: 0,
        totalCostUsd: 0,
        estimatedTotalCost: 3.14,
        estimatedTotalCostCurrency: "CNY",
      },
    });

    render(<AccountCard account={account} />);

    expect(screen.queryByText("5h")).not.toBeInTheDocument();
    expect(screen.queryByText("Weekly")).not.toBeInTheDocument();
    expect(screen.getByText("Tokens (7d)")).toBeInTheDocument();
    expect(screen.getByText("Tokens (Total)")).toBeInTheDocument();
    expect(screen.getByText("Est. Price")).toBeInTheDocument();
  });

  it("renders grouped account availability reasons without a fail count", () => {
    const account = createAccountSummary({
      accountId: "domain:example.com",
      email: "2 available / 7 total",
      displayName: "example.com",
      status: "active",
      availability: {
        total: 7,
        active: 2,
        rateLimited: 1,
        quotaLimited: 2,
        paused: 1,
        deactivated: 0,
      },
    });

    render(<AccountCard account={account} />);

    expect(screen.getByText("2 available / 7 total")).toBeInTheDocument();
    expect(screen.getByText("Rate limit 1")).toBeInTheDocument();
    expect(screen.getByText("Quota/limit 2")).toBeInTheDocument();
    expect(screen.getByText("Paused 1")).toBeInTheDocument();
    expect(screen.queryByText(/fail/i)).not.toBeInTheDocument();
  });

  it("blurs the dashboard card title when privacy mode is enabled", () => {
    act(() => {
      usePrivacyStore.setState({ blurred: true });
    });
    const account = createAccountSummary({
      displayName: "AWS Account MSP",
      email: "aws-account@example.com",
    });

    const { container } = render(<AccountCard account={account} />);

    expect(screen.getByText("AWS Account MSP")).toBeInTheDocument();
    expect(container.querySelector(".privacy-blur")).not.toBeNull();
  });
});
