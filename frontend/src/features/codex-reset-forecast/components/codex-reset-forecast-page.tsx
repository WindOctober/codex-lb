import { Activity, AlertTriangle, Clock3, ExternalLink, Gauge, MessageSquareReply, RefreshCw, Signal, Sparkles } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";

import { AlertMessage } from "@/components/alert-message";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { useCodexResetForecast } from "@/features/codex-reset-forecast/hooks/use-codex-reset-forecast";
import type { CodexResetEvidence, CodexResetHistoricalExample, CodexResetXActivityItem } from "@/features/codex-reset-forecast/schemas";
import { cn } from "@/lib/utils";
import { formatDateTimeInline } from "@/utils/formatters";

const levelStyles = {
  low: "border-emerald-500/20 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
  elevated: "border-amber-500/20 bg-amber-500/10 text-amber-700 dark:text-amber-300",
  high: "border-rose-500/20 bg-rose-500/10 text-rose-700 dark:text-rose-300",
};

const barStyles = {
  low: "[&_[data-slot=progress-indicator]]:bg-emerald-500",
  elevated: "[&_[data-slot=progress-indicator]]:bg-amber-500",
  high: "[&_[data-slot=progress-indicator]]:bg-rose-500",
};

const relevanceStyles = {
  none: "border-slate-500/20 bg-slate-500/10 text-slate-700 dark:text-slate-300",
  weak: "border-amber-500/20 bg-amber-500/10 text-amber-700 dark:text-amber-300",
  strong: "border-rose-500/20 bg-rose-500/10 text-rose-700 dark:text-rose-300",
};

function formatLeadTime(hours: number): string {
  if (hours < 6) {
    return `${hours.toFixed(1)}h`;
  }
  return `${Math.round(hours)}h`;
}

function evidenceMeta(item: CodexResetEvidence): string {
  const parts = [item.source];
  if (item.ageHours !== null) {
    parts.push(`${item.ageHours.toFixed(1)}h old`);
  }
  return parts.join(" / ");
}

function formatOptionalTime(value: string | null): string {
  return value ? formatDateTimeInline(value) : "not yet";
}

function EvidenceRow({ item }: { item: CodexResetEvidence }) {
  return (
    <div className="rounded-lg border border-border/70 bg-card p-4 shadow-[var(--shadow-xs)]">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <Signal className="h-4 w-4 text-primary" aria-hidden="true" />
            <h3 className="text-sm font-semibold">{item.label}</h3>
            <Badge variant="outline" className="rounded-md">
              {Math.round(item.score * 100)} score
            </Badge>
          </div>
          <p className="max-w-3xl text-sm text-muted-foreground">{item.summary}</p>
          <p className="text-xs text-muted-foreground">{evidenceMeta(item)}</p>
        </div>
        {item.url ? (
          <a
            href={item.url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border px-2.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            Open
            <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
          </a>
        ) : null}
      </div>
    </div>
  );
}

function XActivityRow({ item }: { item: CodexResetXActivityItem }) {
  const hasTranslation = item.translatedTextZh.trim() && item.translatedTextZh.trim() !== item.text.trim();
  const parentHasTranslation =
    item.parent !== null && item.parent.translatedTextZh.trim() && item.parent.translatedTextZh.trim() !== item.parent.text.trim();

  return (
    <div className="rounded-lg border border-border/70 bg-card p-4 shadow-[var(--shadow-xs)]">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1 space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <MessageSquareReply className="h-4 w-4 text-primary" aria-hidden="true" />
            <span className="text-sm font-semibold">{item.authorHandle}</span>
            <Badge variant="outline" className="rounded-md capitalize">
              {item.kind}
            </Badge>
            <Badge className={cn("rounded-md border", relevanceStyles[item.resetRelevance])}>
              {item.resetRelevance}
            </Badge>
          </div>
          <div className="max-w-4xl space-y-2">
            <div className="rounded-md bg-muted/35 p-3">
              <p className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">中文</p>
              <p className="mt-1 text-sm leading-6 text-foreground">{hasTranslation ? item.translatedTextZh : item.text}</p>
            </div>
            <div className="rounded-md border border-border/70 p-3">
              <p className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">English original</p>
              <p className="mt-1 text-sm leading-6 text-muted-foreground">{item.text}</p>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
            <span>{formatDateTimeInline(item.observedAt)}</span>
            {item.replyTo ? <span>replying to {item.replyTo}</span> : null}
            <span>{item.relevanceSummary}</span>
          </div>
          {item.parent ? (
            <div className="max-w-4xl rounded-md border border-border/70 bg-muted/20 p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                  Replied to {item.parent.authorHandle}
                </p>
                {item.parent.url ? (
                  <a
                    href={item.parent.url}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                  >
                    Parent
                    <ExternalLink className="h-3 w-3" aria-hidden="true" />
                  </a>
                ) : null}
              </div>
              <p className="mt-2 text-sm leading-6 text-foreground">
                {parentHasTranslation ? item.parent.translatedTextZh : item.parent.text}
              </p>
              <p className="mt-2 text-xs leading-5 text-muted-foreground">{item.parent.text}</p>
            </div>
          ) : null}
        </div>
        <a
          href={item.url}
          target="_blank"
          rel="noreferrer"
          className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border px-2.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        >
          Open
          <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
        </a>
      </div>
    </div>
  );
}

function ExampleRow({ example }: { example: CodexResetHistoricalExample }) {
  return (
    <div className="grid gap-4 rounded-lg border border-border/70 bg-card p-4 shadow-[var(--shadow-xs)] md:grid-cols-[9rem_1fr]">
      <div>
        <Badge variant="secondary" className="rounded-md">
          {formatLeadTime(example.leadTimeHours)}
        </Badge>
        <p className="mt-2 text-xs font-medium text-muted-foreground">{example.classification}</p>
      </div>
      <div className="min-w-0 space-y-3">
        <div>
          <h3 className="text-sm font-semibold">{example.label}</h3>
          <p className="mt-1 text-sm text-muted-foreground">{example.signalSummary}</p>
        </div>
        <div className="grid gap-3 text-xs text-muted-foreground sm:grid-cols-2">
          <a href={example.signalUrl} target="_blank" rel="noreferrer" className="rounded-md bg-muted/50 p-3 hover:text-foreground">
            <span className="block font-medium text-foreground">Signal</span>
            {formatDateTimeInline(example.signalAt)}
          </a>
          <a href={example.resetUrl} target="_blank" rel="noreferrer" className="rounded-md bg-muted/50 p-3 hover:text-foreground">
            <span className="block font-medium text-foreground">Reset</span>
            {formatDateTimeInline(example.resetAt)}
          </a>
        </div>
        <p className="text-sm text-muted-foreground">{example.resetSummary}</p>
      </div>
    </div>
  );
}

export function CodexResetForecastPage() {
  const queryClient = useQueryClient();
  const forecastQuery = useCodexResetForecast();
  const forecast = forecastQuery.data;
  const isRefreshing = forecastQuery.isFetching;
  const errorMessage = forecastQuery.error instanceof Error ? forecastQuery.error.message : null;

  return (
    <div className="animate-fade-in-up space-y-7">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Reset Odds</h1>
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
            OpenAI Codex usage-limit reset forecast for the next 24 hours.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void queryClient.invalidateQueries({ queryKey: ["codex-reset-forecast"] })}
          disabled={isRefreshing}
          className="inline-flex h-8 items-center gap-2 rounded-md border border-border px-3 text-xs font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:pointer-events-none disabled:opacity-50"
        >
          <RefreshCw className={cn("h-3.5 w-3.5", isRefreshing && "animate-spin")} aria-hidden="true" />
          Refresh
        </button>
      </div>

      {errorMessage ? <AlertMessage variant="error">{errorMessage}</AlertMessage> : null}

      {!forecast ? (
        <div className="grid gap-4 lg:grid-cols-[1.2fr_0.8fr]">
          <div className="h-72 rounded-lg border border-border bg-card" />
          <div className="h-72 rounded-lg border border-border bg-card" />
        </div>
      ) : (
        <>
          <section className="grid gap-4 lg:grid-cols-[1.25fr_0.75fr]">
            <div className="rounded-lg border border-border/70 bg-card p-6 shadow-[var(--shadow-sm)]">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-3">
                  <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10 text-primary">
                    <Gauge className="h-5 w-5" aria-hidden="true" />
                  </div>
                  <div>
                    <p className="text-sm font-medium text-muted-foreground">Next {forecast.horizonHours}h probability</p>
                    <p className="text-xs text-muted-foreground">Generated {formatDateTimeInline(forecast.generatedAt)}</p>
                  </div>
                </div>
                <Badge className={cn("rounded-md border", levelStyles[forecast.probabilityLevel])}>
                  {forecast.probabilityLevel}
                </Badge>
              </div>
              <div className="mt-8">
                <div className="flex items-end gap-3">
                  <span className="text-6xl font-semibold tracking-tight">{forecast.probabilityPercent}%</span>
                  <span className="pb-2 text-sm font-medium text-muted-foreground">confidence {forecast.confidence}</span>
                </div>
                <Progress value={forecast.probabilityPercent} className={cn("mt-5 h-3", barStyles[forecast.probabilityLevel])} />
                <p className="mt-5 max-w-3xl text-sm leading-6 text-muted-foreground">{forecast.summary}</p>
              </div>
            </div>

            <div className="rounded-lg border border-border/70 bg-card p-5 shadow-[var(--shadow-sm)]">
              <div className="flex items-center gap-2">
                <Sparkles className="h-4 w-4 text-primary" aria-hidden="true" />
                <h2 className="text-sm font-semibold">Scoring Factors</h2>
              </div>
              <div className="mt-4 space-y-3">
                <div className="rounded-md border border-border/70 bg-muted/35 p-3">
                  <div className="flex items-center justify-between gap-3">
                    <div className="flex items-center gap-2">
                      <Activity className="h-4 w-4 text-primary" aria-hidden="true" />
                      <span className="text-sm font-medium">Live collector</span>
                    </div>
                    <Badge variant="outline" className="rounded-md">
                      {forecast.collectionStatus.refreshInProgress
                        ? "refreshing"
                        : forecast.collectionStatus.refreshEnabled
                          ? "enabled"
                          : "disabled"}
                    </Badge>
                  </div>
                  <div className="mt-3 grid gap-1.5 text-xs text-muted-foreground">
                    <div className="flex justify-between gap-3">
                      <span>Last completed</span>
                      <span className="text-right">{formatOptionalTime(forecast.collectionStatus.lastCompletedAt)}</span>
                    </div>
                    <div className="flex justify-between gap-3">
                      <span>Next refresh</span>
                      <span className="text-right">{formatOptionalTime(forecast.collectionStatus.nextRefreshDueAt)}</span>
                    </div>
                    {forecast.collectionStatus.lastError ? (
                      <p className="break-words pt-1 text-amber-600 dark:text-amber-300">
                        {forecast.collectionStatus.lastError}
                      </p>
                    ) : null}
                  </div>
                </div>
                {forecast.scoreFactors.map((factor) => (
                  <div key={factor.key} className="space-y-1.5">
                    <div className="flex items-center justify-between gap-3 text-sm">
                      <span className="font-medium">{factor.label}</span>
                      <span className="text-muted-foreground">{Math.round(factor.value * 100)}%</span>
                    </div>
                    <Progress value={Math.round(factor.value * 100)} className="h-1.5" />
                    <p className="text-xs text-muted-foreground">{factor.description}</p>
                  </div>
                ))}
              </div>
            </div>
          </section>

          <section className="space-y-4">
            <div className="flex items-center gap-3">
              <h2 className="text-[13px] font-medium uppercase tracking-wider text-muted-foreground">Current Evidence</h2>
              <div className="h-px flex-1 bg-border" />
            </div>
            <div className="grid gap-3">
              {forecast.currentEvidence.map((item) => (
                <EvidenceRow key={`${item.kind}-${item.observedAt ?? item.source}`} item={item} />
              ))}
            </div>
          </section>

          <section className="space-y-4">
            <div className="flex items-center gap-3">
              <h2 className="text-[13px] font-medium uppercase tracking-wider text-muted-foreground">Latest X Replies</h2>
              <div className="h-px flex-1 bg-border" />
            </div>
            <div className="grid gap-3">
              {forecast.latestXItems.length > 0 ? (
                forecast.latestXItems.map((item) => <XActivityRow key={item.url} item={item} />)
              ) : (
                <div className="rounded-lg border border-border/70 bg-card p-4 text-sm text-muted-foreground shadow-[var(--shadow-xs)]">
                  No recent Tibo/Sam X items have been cached by the collector yet.
                </div>
              )}
            </div>
          </section>

          <section className="space-y-4">
            <div className="flex items-center gap-3">
              <h2 className="text-[13px] font-medium uppercase tracking-wider text-muted-foreground">Precursor Examples</h2>
              <div className="h-px flex-1 bg-border" />
            </div>
            <div className="grid gap-3">
              {forecast.historicalExamples.map((example) => (
                <ExampleRow key={example.resetUrl} example={example} />
              ))}
            </div>
          </section>

          <section className="rounded-lg border border-border/70 bg-muted/35 p-4 text-sm text-muted-foreground">
            <div className="flex gap-2">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" aria-hidden="true" />
              <p>{forecast.sourceNote}</p>
            </div>
            <div className="mt-3 flex items-center gap-2 text-xs">
              <Clock3 className="h-3.5 w-3.5" aria-hidden="true" />
              <span>Model {forecast.modelVersion}</span>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
