import { useMemo } from "react";
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceDot,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { useChartColors } from "@/hooks/use-chart-colors";
import { useReducedMotion } from "@/hooks/use-reduced-motion";
import type { AccountQuotaTimelineBucket } from "@/features/accounts/schemas";
import { formatCompactNumber } from "@/utils/formatters";

type TimelinePoint = {
  startAt: string;
  endAt: string;
  primaryUsedPercent: number;
  primaryUsedCredits: number | null;
  secondaryRemainingPercent: number | null;
  secondaryReset: boolean;
};

type ChartTooltipPayloadEntry = {
  dataKey?: string | number;
  value?: number | null;
  color?: string;
  payload?: TimelinePoint;
};

type ChartTooltipProps = {
  active?: boolean;
  payload?: ChartTooltipPayloadEntry[];
  label?: string;
};

const QUOTA_TIMELINE_TIME_ZONE = "Asia/Taipei";
const quotaTimelineTickFormatter = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  timeZone: QUOTA_TIMELINE_TIME_ZONE,
});
const quotaTimelineTooltipFormatter = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
  timeZone: QUOTA_TIMELINE_TIME_ZONE,
});

function formatXTick(isoStr: string): string {
  const d = new Date(isoStr);
  return Number.isNaN(d.getTime()) ? "--" : quotaTimelineTickFormatter.format(d);
}

function formatQuotaTimelineDateTime(isoStr: string | null | undefined): string {
  if (!isoStr) return "--";
  const d = new Date(isoStr);
  return Number.isNaN(d.getTime()) ? "--" : quotaTimelineTooltipFormatter.format(d);
}

function CustomTooltip({ active, payload, label }: ChartTooltipProps) {
  if (!active || !payload?.length) return null;
  const point = payload[0]?.payload;
  const heading = `${formatQuotaTimelineDateTime(label)} - ${
    point ? formatQuotaTimelineDateTime(point.endAt) : ""
  }`;
  const primary = point?.primaryUsedPercent ?? 0;
  const credits = point?.primaryUsedCredits;
  const secondary = point?.secondaryRemainingPercent;
  return (
    <div className="rounded-lg border bg-popover px-3 py-2 text-popover-foreground shadow-md">
      <p className="mb-1 text-[11px] text-muted-foreground">{heading}</p>
      <div className="flex items-center gap-2 text-xs">
        <span className="inline-block h-2 w-2 rounded-sm bg-chart-1" />
        <span className="text-muted-foreground">5h used</span>
        <span className="ml-auto tabular-nums font-medium">
          {primary.toFixed(1)}%
          {credits !== null && credits !== undefined ? ` / ${formatCompactNumber(credits)} cr` : ""}
        </span>
      </div>
      <div className="flex items-center gap-2 text-xs">
        <span className="inline-block h-2 w-2 rounded-full bg-chart-2" />
        <span className="text-muted-foreground">Weekly left</span>
        <span className="ml-auto tabular-nums font-medium">
          {secondary === null || secondary === undefined ? "--" : `${secondary.toFixed(1)}%`}
        </span>
      </div>
      {point?.secondaryReset ? <p className="mt-1 text-[11px] font-medium text-primary">7d reset</p> : null}
    </div>
  );
}

const CHART_MARGIN = { top: 8, right: 8, bottom: 0, left: 0 } as const;

export type AccountQuotaTimelineChartProps = {
  buckets: AccountQuotaTimelineBucket[];
};

export function AccountQuotaTimelineChart({ buckets }: AccountQuotaTimelineChartProps) {
  const chartColors = useChartColors();
  const reducedMotion = useReducedMotion();
  const primaryColor = chartColors[0];
  const secondaryColor = chartColors[1];
  const resetColor = chartColors[2] ?? "hsl(var(--primary))";
  const data = useMemo<TimelinePoint[]>(
    () =>
      buckets.map((bucket) => ({
        startAt: bucket.startAt,
        endAt: bucket.endAt,
        primaryUsedPercent: bucket.primaryUsedPercent,
        primaryUsedCredits: bucket.primaryUsedCredits ?? null,
        secondaryRemainingPercent: bucket.secondaryRemainingPercent ?? null,
        secondaryReset: bucket.secondaryReset,
      })),
    [buckets],
  );

  if (data.length === 0) {
    return (
      <div className="flex h-[220px] items-center justify-center text-xs text-muted-foreground">
        No quota timeline available
      </div>
    );
  }

  return (
    <ResponsiveContainer width="100%" height={220}>
      <ComposedChart data={data} margin={CHART_MARGIN}>
        <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="currentColor" opacity={0.06} />
        <XAxis
          dataKey="startAt"
          tickFormatter={formatXTick}
          tick={{ fontSize: 10, fill: "var(--muted-foreground)" }}
          tickLine={false}
          axisLine={false}
          minTickGap={44}
          dy={4}
        />
        <YAxis
          domain={[0, 100]}
          ticks={[0, 25, 50, 75, 100]}
          tickFormatter={(v: number) => `${v}%`}
          tick={{ fontSize: 10, fill: "var(--muted-foreground)" }}
          tickLine={false}
          axisLine={false}
          width={38}
        />
        <Tooltip content={<CustomTooltip />} cursor={{ fill: "hsl(var(--muted) / 0.35)" }} />
        <Bar
          dataKey="primaryUsedPercent"
          fill={primaryColor}
          radius={[2, 2, 0, 0]}
          maxBarSize={18}
          isAnimationActive={!reducedMotion}
          animationDuration={350}
        />
        <Line
          type="monotone"
          dataKey="secondaryRemainingPercent"
          stroke={secondaryColor}
          strokeWidth={1.7}
          dot={false}
          connectNulls
          activeDot={{ r: 3, strokeWidth: 1.5, fill: "hsl(var(--popover))" }}
          isAnimationActive={!reducedMotion}
          animationDuration={450}
        />
        {data
          .filter((point) => point.secondaryReset && point.secondaryRemainingPercent !== null)
          .map((point) => (
            <ReferenceDot
              key={point.startAt}
              x={point.startAt}
              y={point.secondaryRemainingPercent ?? 0}
              r={4}
              fill={resetColor}
              stroke="hsl(var(--background))"
              strokeWidth={1.5}
            />
          ))}
      </ComposedChart>
    </ResponsiveContainer>
  );
}
