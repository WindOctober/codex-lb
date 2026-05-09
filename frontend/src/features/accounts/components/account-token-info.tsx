import { CopyButton } from "@/components/copy-button";
import type { AccountSummary } from "@/features/accounts/schemas";
import {
  formatAccessTokenLabel,
  formatIdTokenLabel,
  formatRefreshTokenLabel,
} from "@/utils/formatters";

export type AccountTokenInfoProps = {
  account: AccountSummary;
};

export function AccountTokenInfo({ account }: AccountTokenInfoProps) {
  if ((account.providerKind ?? "openai_oauth") === "api_key") {
    return (
      <div className="space-y-3 rounded-lg border bg-muted/30 p-4">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">API Key</h3>
        <div className="flex items-center justify-between gap-3 text-xs">
          <span className="truncate font-mono text-muted-foreground">
            {account.storedApiKey ? `${account.storedApiKey.slice(0, 15)}...` : "Stored"}
          </span>
          {account.storedApiKey ? <CopyButton value={account.storedApiKey} label="Copy provider API key" /> : null}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-3 rounded-lg border bg-muted/30 p-4">
      <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Token Status</h3>
      <dl className="space-y-2 text-xs">
        <div className="flex items-center justify-between gap-2">
          <dt className="text-muted-foreground">Access</dt>
          <dd className="font-medium">{formatAccessTokenLabel(account.auth)}</dd>
        </div>
        <div className="flex items-center justify-between gap-2">
          <dt className="text-muted-foreground">Refresh</dt>
          <dd className="font-medium">{formatRefreshTokenLabel(account.auth)}</dd>
        </div>
        <div className="flex items-center justify-between gap-2">
          <dt className="text-muted-foreground">ID token</dt>
          <dd className="font-medium">{formatIdTokenLabel(account.auth)}</dd>
        </div>
      </dl>
    </div>
  );
}
