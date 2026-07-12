import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AccountQuotaTimelineChart } from "@/features/accounts/components/account-quota-timeline-chart";

vi.mock("recharts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("recharts")>();
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
      <div data-testid="responsive-container" style={{ width: 400, height: 220 }}>
        {children}
      </div>
    ),
  };
});

const BASE = new Date("2026-01-15T00:00:00Z");

function makeBuckets(count: number) {
  return Array.from({ length: count }, (_, i) => ({
    startAt: new Date(BASE.getTime() + i * 5 * 3600_000).toISOString(),
    endAt: new Date(BASE.getTime() + (i + 1) * 5 * 3600_000).toISOString(),
    primaryUsedPercent: 20 + i,
    primaryUsedCredits: 20 + i,
    secondaryRemainingPercent: 80 - i,
    secondaryReset: i === 2,
    secondaryResetAt: i === 2 ? new Date(BASE.getTime() + i * 5 * 3600_000).toISOString() : null,
  }));
}

describe("AccountQuotaTimelineChart", () => {
  it("renders empty state when no data is provided", () => {
    render(<AccountQuotaTimelineChart buckets={[]} />);
    expect(screen.getByText("No quota timeline available")).toBeInTheDocument();
  });

  it("renders chart container when timeline buckets are provided", () => {
    render(<AccountQuotaTimelineChart buckets={makeBuckets(8)} />);
    expect(screen.getByTestId("responsive-container")).toBeInTheDocument();
  });
});
