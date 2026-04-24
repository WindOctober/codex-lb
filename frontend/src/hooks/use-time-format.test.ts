import { beforeEach, describe, expect, it } from "vitest";

import { getTimeFormatPreference, useTimeFormatStore } from "@/hooks/use-time-format";

describe("useTimeFormatStore", () => {
  beforeEach(() => {
    window.localStorage.clear();
    useTimeFormatStore.setState({ timeFormat: "24h" });
  });

  it("defaults to 24h", () => {
    expect(getTimeFormatPreference()).toBe("24h");
  });

  it("persists updates to localStorage", () => {
    useTimeFormatStore.getState().setTimeFormat("24h");

    expect(getTimeFormatPreference()).toBe("24h");
    expect(window.localStorage.getItem("codex-lb-time-format")).toBe("24h");
  });
});
