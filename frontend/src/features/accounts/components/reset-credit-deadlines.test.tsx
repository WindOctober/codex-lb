import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ResetCreditDeadlines } from "@/features/accounts/components/reset-credit-deadlines";

describe("ResetCreditDeadlines", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-13T00:00:00.000Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows every exact deadline with readable urgency", () => {
    render(
      <ResetCreditDeadlines
        credits={[
          {
            resetType: "codex_rate_limits",
            title: "Soon reset",
            grantedAt: "2026-06-14T03:00:00Z",
            expiresAt: "2026-07-14T03:00:00Z",
          },
          {
            resetType: "codex_rate_limits",
            title: "Later reset",
            grantedAt: "2026-06-18T03:00:00Z",
            expiresAt: "2026-07-18T03:00:00Z",
          },
        ]}
      />,
    );

    expect(screen.getByText("Soon reset")).toBeInTheDocument();
    expect(screen.getByText("Later reset")).toBeInTheDocument();
    expect(screen.getByText("1d 3h left")).toHaveClass("text-amber-700");
    expect(screen.getByText("5d 3h left")).toHaveClass("text-emerald-700");
    expect(screen.getAllByText(/^Expires /)).toHaveLength(2);
    expect(screen.getAllByText(/^Expires /)[0]).toHaveAttribute(
      "datetime",
      "2026-07-14T03:00:00Z",
    );
  });

  it("keeps a credit visible when its deadline is unavailable", () => {
    render(
      <ResetCreditDeadlines
        credits={[
          {
            resetType: "codex_rate_limits",
            title: null,
            grantedAt: null,
            expiresAt: null,
          },
        ]}
      />,
    );

    expect(screen.getByText("Codex Rate Limits")).toBeInTheDocument();
    expect(screen.getByText("Expiry not provided")).toBeInTheDocument();
    expect(screen.getByText("Deadline unavailable")).toBeInTheDocument();
  });

  it("marks an elapsed deadline as expired", () => {
    render(
      <ResetCreditDeadlines
        credits={[
          {
            resetType: "codex_rate_limits",
            title: "Elapsed reset",
            grantedAt: "2026-06-01T00:00:00Z",
            expiresAt: "2026-07-12T23:59:00Z",
          },
        ]}
      />,
    );

    expect(screen.getByText("Expired")).toHaveClass("text-red-700");
  });
});
