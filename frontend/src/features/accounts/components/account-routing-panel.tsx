import { Activity, Save } from "lucide-react";
import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import type { AccountSummary } from "@/features/accounts/schemas";
import { formatSlug } from "@/utils/formatters";

export type AccountRoutingPanelProps = {
  account: AccountSummary;
  busy: boolean;
  onUpdateRouting: (accountId: string, configuredPriority: number, kycEnabled?: boolean) => Promise<void>;
  onTestAvailability: (accountId: string) => Promise<void>;
};

export function AccountRoutingPanel({
  account,
  busy,
  onUpdateRouting,
  onTestAvailability,
}: AccountRoutingPanelProps) {
  const configuredPriority = account.configuredPriority ?? 0;
  const providerKind = account.providerKind ?? "openai_oauth";
  const routingTier = account.routingTier ?? "openai_paid";
  const routingPriority = account.routingPriority ?? configuredPriority;
  const [priority, setPriority] = useState(String(configuredPriority));

  useEffect(() => {
    setPriority(String(configuredPriority));
  }, [account.accountId, configuredPriority]);

  const parsedPriority = Number.parseInt(priority, 10);
  const priorityChanged =
    Number.isInteger(parsedPriority) && parsedPriority !== configuredPriority && parsedPriority >= 0;
  const kycEnabled = account.kycEnabled ?? false;

  return (
    <section className="rounded-lg border bg-muted/20 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold">Routing</h3>
        <Badge variant={providerKind === "api_key" ? "default" : "secondary"}>
          {providerKind === "api_key" ? "API provider" : "OpenAI OAuth"}
        </Badge>
        <Badge variant="outline">{formatSlug(routingTier)}</Badge>
      </div>

      <div className="mt-3 grid gap-3 sm:grid-cols-[minmax(0,12rem)_auto_auto] sm:items-end">
        <div className="space-y-1.5">
          <Label htmlFor={`priority-${account.accountId}`}>Priority</Label>
          <Input
            id={`priority-${account.accountId}`}
            type="number"
            min={0}
            max={100000}
            value={priority}
            onChange={(event) => setPriority(event.target.value)}
          />
        </div>

        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-9 gap-1.5"
          disabled={busy || !priorityChanged}
          onClick={() => void onUpdateRouting(account.accountId, parsedPriority, kycEnabled)}
        >
          <Save className="h-3.5 w-3.5" />
          Save Priority
        </Button>

        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-9 gap-1.5"
          disabled={busy}
          onClick={() => onTestAvailability(account.accountId)}
        >
          <Activity className="h-3.5 w-3.5" />
          Test Availability
        </Button>
      </div>

      <p className="mt-2 text-xs text-muted-foreground">
        Effective routing rank: {routingPriority}. Lower priority values are selected first.
      </p>

      <div className="mt-3 flex items-center justify-between gap-4 rounded-lg border bg-background/60 px-3 py-2">
        <div>
          <p className="text-sm font-medium">KYC account</p>
          <p className="text-xs text-muted-foreground">
            When KYC routing enforcement is enabled, only KYC-only API keys can use this account.
          </p>
        </div>
        <Switch
          checked={kycEnabled}
          disabled={busy}
          onCheckedChange={(checked) => void onUpdateRouting(account.accountId, configuredPriority, checked)}
        />
      </div>
    </section>
  );
}
