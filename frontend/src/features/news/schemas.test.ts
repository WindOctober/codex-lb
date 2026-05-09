import { describe, expect, it } from "vitest";

import { NewsSnapshotSchema } from "@/features/news/schemas";

describe("NewsSnapshotSchema", () => {
  it("parses X AI dynamics and hotspot digest panels", () => {
    const parsed = NewsSnapshotSchema.parse({
      status: "ready",
      x_ai_dynamics: {
        generated_at: "2026-05-02T00:00:00+00:00",
        headline: "X AI 快讯",
        summary: "X 上 AI 动态集中在模型和 coding agents。",
        items: [
          {
            headline: "OpenAI 发布新动态",
            url: "https://x.com/openai/status/1",
            category: "模型",
            is_new: true,
          },
        ],
      },
      hotspot_digest: {
        generated_at: "2026-05-02T00:00:00+00:00",
        headline: "全网出行热点升温",
        summary: "多平台热榜集中在假期出行。",
        clusters: [
          {
            title: "假期出行",
            summary: "景区拥堵成为跨平台主线。",
            mood: "焦虑",
          },
        ],
        top_items: [
          {
            title: "堵山堵海堵桥堵路",
            platform: "百度热搜",
            rank: 1,
            url: "https://www.baidu.com/s?wd=test",
          },
        ],
      },
    });

    expect(parsed.x_ai_dynamics.items[0].headline).toBe("OpenAI 发布新动态");
    expect(parsed.hotspot_digest.clusters[0].title).toBe("假期出行");
    expect(parsed.hotspot_digest.top_items[0].rank).toBe(1);
  });
});
